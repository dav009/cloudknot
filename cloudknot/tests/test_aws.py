"""Test classes, functions, etc. in the aws submodule

This file contains tests for classes, functions, etc. in the aws submodule.
Each of the classes in the AWS submodule represents an AWS resource.
The tests for each resource all follow the same pattern:

* Smoke testing:
  - Use boto3 to create a resource.
  - Use cloudknot to create a resource with same name and different properties.
  - Assert that cloudknot resource instance raises an ResourceExistsException.
  - Use cloudknot to create a resource instance with only the (name, ARN, etc.)
    input of the pre-existing resource (no conflicting parameter info).
  - Confirm that the cloudknot resource instance has the right properties.
  - Confirm that the resource is now in the config file.
  - Create some more resource instances from scratch, perhaps with different
    input values.
  - Use boto3 to confirm their existence and properties.
  - Confirm that they now exist in the config file.
  - Clobber the resources we created.
  - Use boto3 to confirm that they don't exist anymore.
  - Confirm that they were removed from the config file.
* Other tests of improper input
"""
from __future__ import absolute_import, division, print_function

import configparser
import json
import pytest
import uuid

import boto3
import cloudknot as ck


def test_IamRole():
    # Use boto3 to create a role
    iam = boto3.client('iam')
    config = configparser.ConfigParser()
    config_file = ck.config.get_config_file()

    try:
        name = 'cloudknot-unit-test-' + str(uuid.uuid4())

        role_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "batch.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole"
                }
            ]
        }
        policy = {
            'name': 'AWSLambdaRole',
            'arn': 'arn:aws:iam::aws:policy/service-role/AWSLambdaRole'
        }

        response = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(role_policy),
        )
        arn = response.get('Role')['Arn']

        iam.attach_role_policy(
            PolicyArn=policy['arn'],
            RoleName=name
        )

        # Create an IamRole with same name and different properties.
        # Confirm that IamRole raises a ResourceExistsException.
        with pytest.raises(ck.aws.ResourceExistsException) as e:
            ck.aws.IamRole(name=name, service='ec2')

        assert e.value.resource_id == name

        # Then create an IamRole with only that name or ARN to have cloudknot
        # retrieve that role.
        role = ck.aws.IamRole(name=name)

        # Confirm that the instance has the right properties.
        assert role.service is None
        assert role.arn == arn
        assert role.policies == (policy['name'],)

        # Confirm that the role is in the config file
        config.read(config_file)
        assert name in config.options('roles')

        # Clobber the role
        role.clobber()
        # Assert that it was removed from AWS
        with pytest.raises(iam.exceptions.NoSuchEntityException):
            iam.get_role(RoleName=name)

        # Assert that it was removed from the config file
        # If we just re-read the config file, config will keep the union
        # of the in memory values and the file values, updating the
        # intersection of the two with the file values. So we must set
        # config to None and then re-read the file
        config = None
        config = configparser.ConfigParser()
        config.read(config_file)
        assert name not in config.options('roles')

        # Try to retrieve a role that does not exist
        name = 'cloudknot-unit-test-' + str(uuid.uuid4())
        with pytest.raises(ck.aws.ResourceDoesNotExistException) as e:
            ck.aws.IamRole(name=name)

        assert e.value.resource_id == name

        # Create two roles, one with an instance profile and one without.
        names = ['cloudknot-unit-test-' + str(uuid.uuid4()) for i in range(2)]
        descriptions = ['Role for unit testing of cloudknot.aws.IamRole()', None]
        services = ['ec2', 'ecs-tasks']
        policy_set = ['AmazonS3FullAccess',
                      ['AWSLambdaExecute', 'AmazonS3ReadOnlyAccess']]
        instance_profile_flags = [True, False]

        for (n, d, s, p, i) in zip(
            names, descriptions, services, policy_set, instance_profile_flags
        ):
            role = ck.aws.IamRole(name=n, description=d, service=s,
                                  policies=p, add_instance_profile=i)

            # Use boto3 to confirm their existence and properties
            assert role.name == n
            d = d if d else 'This role was generated by cloudknot'
            assert role.description == d
            assert role.service == s + '.amazonaws.com'
            p = (p,) if isinstance(p, str) else tuple(p)
            assert set(role.policies) == set(p)
            assert role.add_instance_profile == i
            if i:
                assert role.instance_profile_arn
            else:
                assert role.instance_profile_arn is None

            # Confirm that they exist in the config file
            config.read(config_file)
            assert n in config.options('roles')

            # Clobber roles and use boto3 to confirm that they don't exist
            role.clobber()
            with pytest.raises(iam.exceptions.NoSuchEntityException):
                iam.get_role(RoleName=n)

            # Assert that they were removed from the config file
            # If we just re-read the config file, config will keep the union
            # of the in memory values and the file values, updating the
            # intersection of the two with the file values. So we must set
            # config to None and then re-read the file
            config = None
            config = configparser.ConfigParser()
            config.read(config_file)
            assert n not in config.options('roles')

        # Test for correct handling of incorrect input
        with pytest.raises(ValueError) as e:
            ck.aws.IamRole(name='not-important', service='value-error')
        with pytest.raises(ValueError) as e:
            ck.aws.IamRole(name='not-important', service='ec2', policies=455)
        with pytest.raises(ValueError) as e:
            ck.aws.IamRole(name='not-important', service='ec2',
                           policies=[455, 455])
        with pytest.raises(ValueError) as e:
            ck.aws.IamRole(name='not-important', service='ec2',
                           policies='NotAnAWSPolicy')
        with pytest.raises(ValueError) as e:
            ck.aws.IamRole(name='not-important', service='ec2',
                           add_instance_profile=455)

    except Exception as e:
        # Clean up roles from AWS
        # Find all unit test roles
        response = iam.list_roles()
        role_names = [d['RoleName'] for d in response.get('Roles')]
        unit_test_roles = filter(
            lambda n: 'cloudknot-unit-test' in n,
            role_names
        )

        for role_name in unit_test_roles:
            # Remove instance profiles
            response = iam.list_instance_profiles_for_role(RoleName=role_name)
            for ip in response.get('InstanceProfiles'):
                iam.remove_role_from_instance_profile(
                    InstanceProfileName=ip['InstanceProfileName'],
                    RoleName=role_name
                )
                iam.delete_instance_profile(
                    InstanceProfileName=ip['InstanceProfileName']
                )

            # Detach policies from role
            response = iam.list_attached_role_policies(RoleName=role_name)
            for policy in response.get('AttachedPolicies'):
                iam.detach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy['PolicyArn']
                )

            # Delete role
            iam.delete_role(RoleName=role_name)

        # Clean up config file
        config.read(config_file)
        for role_name in config.options('roles'):
            if 'cloudknot-unit-test' in role_name:
                config.remove_option('roles', role_name)
        with open(config_file, 'w') as f:
            config.write(f)

        # Pass the exception through
        raise e


def test_Vpc():
    # # Use boto3 to create a role
    # ec2 = boto3.client('ec2')
    # config = configparser.ConfigParser()
    # config_file = ck.config.get_config_file()
    #
    # try:
    #     response = EC2.create_vpc(
    #         CidrBlock='10.0.0.0/16',
    #         AmazonProvidedIpv6CidrBlock=True,
    #         InstanceTenancy='dedicated'
    #     )
    #
    #     vpc_id = response.get('Vpc')['VpcId']
    #
    #     # Create a IamRole with same name and different properties.
    #     # Confirm that IamRole raises a ResourceExistsException.
    #     with pytest.raises(ck.aws.ResourceExistsException) as e:
    #         ck.aws.Vpc(name=name, service='ec2')
    #
    #     assert e.value.resource_id == vpc_id
    #
    #     # Then create an IamRole with only that name or ARN to have cloudknot
    #     # retrieve that role.
    #     role = ck.aws.IamRole(name=name)
    #
    #     # Confirm that the instance has the right properties.
    #     assert role.service is None
    #     assert role.arn == arn
    #     assert role.policies == (policy['name'],)
    #
    #     # Confirm that the role is in the config file
    #     config.read(config_file)
    #     assert name in config.options('roles')
    #
    #     # Clobber the role
    #     role.clobber()
    #     # Assert that it was removed from AWS
    #     with pytest.raises(ec2.exceptions.NoSuchEntityException):
    #         ec2.get_role(RoleName=name)
    #
    #     # Assert that it was removed from the config file
    #     # Confirm that they were removed from the config file
    #     # If we just re-read the config file, config will keep the union
    #     # of the in memory values and the file values, updating the
    #     # intersection of the two with the file values. So we must set
    #     # config to None and then re-read the file
    #     config = None
    #     config = configparser.ConfigParser()
    #     config.read(config_file)
    #     assert name not in config.options('roles')
    #
    #     # Try to retrieve a role that does not exist
    #     name = 'cloudknot-unit-test-' + str(uuid.uuid4())
    #     with pytest.raises(ck.aws.ResourceDoesNotExistException) as e:
    #         ck.aws.IamRole(name=name)
    #
    #     assert e.value.resource_id == name
    #
    #     # Create two roles, one with an instance profile and one without.
    #     names = ['cloudknot-unit-test-' + str(uuid.uuid4()) for i in range(2)]
    #     descriptions = ['Role for unit testing of cloudknot.aws.IamRole()', None]
    #     services = ['ec2', 'ecs-tasks']
    #     policy_set = ['AmazonS3FullAccess',
    #                   ['AWSLambdaExecute', 'AmazonS3ReadOnlyAccess']]
    #     instance_profile_flags = [True, False]
    #
    #     for (n, d, s, p, i) in zip(
    #             names, descriptions, services, policy_set, instance_profile_flags
    #     ):
    #         role = ck.aws.IamRole(name=n, description=d, service=s,
    #                                    policies=p, add_instance_profile=i)
    #
    #         # Use boto3 to confirm their existence and properties
    #         assert role.name == n
    #         d = d if d else 'This role was generated by cloudknot'
    #         assert role.description == d
    #         assert role.service == s + '.amazonaws.com'
    #         p = (p,) if isinstance(p, str) else tuple(p)
    #         assert set(role.policies) == set(p)
    #         assert role.add_instance_profile == i
    #         if i:
    #             assert role.instance_profile_arn
    #         else:
    #             assert role.instance_profile_arn is None
    #
    #         # Confirm that they exist in the config file
    #         config.read(config_file)
    #         assert n in config.options('roles')
    #
    #         # Clobber roles and use boto3 to confirm that they don't exist
    #         role.clobber()
    #         with pytest.raises(ec2.exceptions.NoSuchEntityException):
    #             ec2.get_role(RoleName=n)
    #
    #         # Assert that they were removed from the config file
    #         # If we just re-read the config file, config will keep the union
    #         # of the in memory values and the file values, updating the
    #         # intersection of the two with the file values. So we must set
    #         # config to None and then re-read the file
    #         config = None
    #         config = configparser.ConfigParser()
    #         config.read(config_file)
    #         assert n not in config.options('roles')
    #
    #     # Test for correct handling of incorrect input
    #     with pytest.raises(ValueError) as e:
    #         ck.aws.IamRole(name='not-important', service='value-error')
    #     with pytest.raises(ValueError) as e:
    #         ck.aws.IamRole(name='not-important', service='ec2', policies=455)
    #     with pytest.raises(ValueError) as e:
    #         ck.aws.IamRole(name='not-important', service='ec2',
    #                        policies=[455, 455])
    #     with pytest.raises(ValueError) as e:
    #         ck.aws.IamRole(name='not-important', service='ec2',
    #                        policies='NotAnAWSPolicy')
    #     with pytest.raises(ValueError) as e:
    #         ck.aws.IamRole(name='not-important', service='ec2',
    #                        add_instance_profile=455)
    #
    # except Exception as e:
    #     # Clean up roles from AWS
    #     # Find all unit test roles
    #     response = ec2.list_roles()
    #     role_names = [d['RoleName'] for d in response.get('Roles')]
    #     unit_test_roles = filter(
    #         lambda n: 'cloudknot-unit-test' in n,
    #         role_names
    #     )
    #
    #     for role_name in unit_test_roles:
    #         # Remove instance profiles
    #         response = ec2.list_instance_profiles_for_role(RoleName=role_name)
    #         for ip in response.get('InstanceProfiles'):
    #             ec2.remove_role_from_instance_profile(
    #                 InstanceProfileName=ip['InstanceProfileName'],
    #                 RoleName=role_name
    #             )
    #             ec2.delete_instance_profile(
    #                 InstanceProfileName=ip['InstanceProfileName']
    #             )
    #
    #         # Detach policies from role
    #         response = ec2.list_attached_role_policies(RoleName=role_name)
    #         for policy in response.get('AttachedPolicies'):
    #             ec2.detach_role_policy(
    #                 RoleName=role_name,
    #                 PolicyArn=policy['PolicyArn']
    #             )
    #
    #         # Delete role
    #         ec2.delete_role(RoleName=role_name)
    #
    #     # Clean up config file
    #     config.read(config_file)
    #     for role_name in config.options('roles'):
    #         if 'cloudknot-unit-test' in role_name:
    #             config.remove_option('roles', role_name)
    #     with open(config_file, 'w') as f:
    #         config.write(f)
    #
    #     # Pass the exception through
    #     raise e
    pass


def test_SecurityGroup():
    # Use boto3 to create a security group
    ec2 = boto3.client('ec2')
    config = configparser.ConfigParser()
    config_file = ck.config.get_config_file()

    try:
        name = 'cloudknot-unit-test-' + str(uuid.uuid4())
        description = 'Security group for cloudknot unit testing'

        # Create a VPC to attach the security group to
        response = ec2.create_vpc(CidrBlock='10.0.0.0/16')
        vpc_id = response.get('Vpc')['VpcId']

        response = ec2.create_security_group(
            GroupName=name,
            Description=description,
            VpcId=vpc_id
        )
        group_id = response.get('GroupId')

        # Tag the VPC and security group for easy cleanup later
        ec2.create_tags(
            Resources=[vpc_id, group_id],
            Tags=[{
                'Key': 'owner',
                'Value': 'cloudknot-security-group-unit-test'
            }]
        )

        # Create a Vpc instance for the same Vpc that we just created
        vpc = ck.aws.Vpc(vpc_id=vpc_id)

        # Create a SecurityGroup with same name but different description.
        # Confirm that SecurityGroup raises a ResourceExistsException.
        with pytest.raises(ck.aws.ResourceExistsException) as e:
            ck.aws.SecurityGroup(
                name=name,
                vpc=vpc,
                description='conflicting description'
            )

        assert e.value.resource_id == group_id

        # Then create a SecurityGroup with only that group_id to have
        # cloudknot retrieve that role.
        sg = ck.aws.SecurityGroup(security_group_id=group_id)

        # Confirm that the instance has the right properties.
        assert sg.pre_existing
        assert sg.vpc is None
        assert sg.vpc_id == vpc_id
        assert sg.description == description
        assert sg.security_group_id == group_id

        # Confirm that the role is in the config file
        config.read(config_file)
        assert group_id in config.options('security-groups')

        # Clobber the role
        sg.clobber()

        # Assert that it was removed from AWS
        with pytest.raises(ec2.exceptions.ClientError) as e:
            ec2.describe_security_groups(GroupIds=[group_id])

        assert e.value.response.get('Error')['Code'] == 'InvalidGroup.NotFound'

        # Assert that it was removed from the config file
        # If we just re-read the config file, config will keep the union
        # of the in memory values and the file values, updating the
        # intersection of the two with the file values. So we must set
        # config to None and then re-read the file
        config = None
        config = configparser.ConfigParser()
        config.read(config_file)
        assert group_id not in config.options('security-groups')

        # Try to retrieve a security group that does not exist
        group_id = 'cloudknot-unit-test-' + str(uuid.uuid4())
        with pytest.raises(ck.aws.ResourceDoesNotExistException) as e:
            ck.aws.SecurityGroup(security_group_id=group_id)

        assert e.value.resource_id == group_id

        # Create two roles, one with an instance profile and one without.
        names = ['cloudknot-unit-test-' + str(uuid.uuid4()) for i in range(2)]
        vpcs = [vpc, vpc]
        descriptions = [
            'Security Group for unit testing of cloudknot.aws.SecurityGroup()',
            None
        ]

        for idx, (n, v, d) in enumerate(zip(
                names, vpcs, descriptions
        )):
            sg = ck.aws.SecurityGroup(
                name=n, vpc=v, description=d
            )

            # Use boto3 to confirm their existence and properties
            assert sg.name == n
            d = d if d else \
                'This security group was automatically generated by cloudknot.'
            assert sg.description == d
            assert sg.vpc == vpc
            assert sg.vpc_id == vpc.vpc_id

            # Confirm that they exist in the config file
            config.read(config_file)
            assert sg.security_group_id in config.options('security-groups')

            # Clobber security group
            sg.clobber()

            # Assert that it was removed from AWS
            with pytest.raises(ec2.exceptions.ClientError) as e:
                ec2.describe_security_groups(GroupIds=[sg.security_group_id])

            error_code = e.value.response.get('Error')['Code']
            assert error_code == 'InvalidGroup.NotFound'

            # Assert that they were removed from the config file
            # If we just re-read the config file, config will keep the union
            # of the in memory values and the file values, updating the
            # intersection of the two with the file values. So we must set
            # config to None and then re-read the file
            config = None
            config = configparser.ConfigParser()
            config.read(config_file)
            assert n not in config.options('roles')

        # Test for correct handling of incorrect input
        with pytest.raises(ValueError) as e:
            ck.aws.SecurityGroup()
        with pytest.raises(ValueError) as e:
            ck.aws.SecurityGroup(
                security_group_id='causes-error', name='causes-error'
            )
        with pytest.raises(ValueError) as e:
            ck.aws.SecurityGroup(name='not-important', vpc=5)

    except Exception as e:
        # Clean up security_groups and VPCs from AWS
        # Find all unit test security groups
        response = ec2.describe_security_groups()
        sgs = [{'name': d['GroupName'], 'id': d['GroupId']}
                    for d in response.get('SecurityGroups')]
        unit_test_sgs = filter(
            lambda d: 'cloudknot-unit-test' in d['name'],
            sgs
        )

        config.read(config_file)

        for sg in unit_test_sgs:
            # Delete role
            ec2.delete_security_group(GroupId=sg['id'])

            # Clean up config file
            config.remove_option('security-groups', sg['id'])

        with open(config_file, 'w') as f:
            config.write(f)

        # Find all VPCs with tag owner = 'cloudknot-security-group-unit-test
        response = ec2.describe_vpcs(
            Filters=[{
                'Name': 'tag:owner',
                'Values': ['cloudknot-security-group-unit-test']
            }]
        )

        for vpc in response.get('Vpcs'):
            ec2.delete_vpc(VpcId=vpc['VpcId'])

        # Pass the exception through
        raise e


def test_JobDefinition():
    pass


def test_JobQueue():
    pass


def test_ComputeEnvironment():
    pass


def test_BatchJob():
    pass


def test_DockerImage():
    pass