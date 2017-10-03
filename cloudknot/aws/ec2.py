from __future__ import absolute_import, division, print_function

import botocore
import cloudknot.config
import ipaddress
import logging
import operator
import six
import tenacity
import time
from collections import namedtuple
from math import ceil

from .base_classes import EC2, BATCH, NamedObject, \
    ResourceExistsException, ResourceDoesNotExistException, \
    CannotDeleteResourceException, wait_for_compute_environment

try:
    from math import log2
except ImportError:  # pragma: nocover
    # python 2.7 compatibility
    from math import log

__all__ = ["Vpc", "SecurityGroup"]


# noinspection PyPropertyAccess,PyAttributeOutsideInit
class Vpc(NamedObject):
    """Class for defining an Amazon Virtual Private Cloud (VPC)"""
    def __init__(self, vpc_id=None, name=None, ipv4_cidr=None,
                 instance_tenancy=None):
        """Initialize a Vpc instance

        Parameters
        ----------
        vpc_id : string
            VPC-ID for the VPC to be retrieved

        name : string
            Name of the VPC to be retrieved or created

        ipv4_cidr : string
            IPv4 CIDR block to be used for creation of a new VPC

        instance_tenancy : string
            Instance tenancy for this VPC, one of ['default', 'dedicated']
            Default: 'default'
        """
        # If user supplies vpc_id, then no other input is allowed
        if not (vpc_id or name):
            raise ValueError('name or vpc_id is required.')

        # If user supplies vpc_id, then no other input is allowed
        if vpc_id and any([name, ipv4_cidr, instance_tenancy]):
            raise ValueError(
                'You must specify either a VPC id for an existing VPC or '
                'input parameters for a new VPC. You cannot do both.'
            )

        # Check for pre-existence based on vpc_id or name
        resource = self._exists_already(vpc_id, name)
        self._pre_existing = resource.exists

        if resource.exists:
            # If resource exists and user supplied an ipv4, abort
            if ipv4_cidr or instance_tenancy:
                raise ResourceExistsException(
                    'The specified ipv4 CIDR block is already in use by '
                    'vpc {id:s}'.format(id=resource.vpc_id),
                    resource_id=resource.vpc_id
                )

            super(Vpc, self).__init__(name=resource.name)

            self._vpc_id = resource.vpc_id
            self._ipv4_cidr = resource.ipv4_cidr
            self._instance_tenancy = resource.instance_tenancy
            self._subnet_ids = resource.subnet_ids

            cloudknot.config.add_resource('vpc', self.vpc_id, self.name)

            logging.info('Retrieved pre-existing VPC {id:s}'.format(
                id=self.vpc_id
            ))
        else:
            if vpc_id:
                raise ResourceDoesNotExistException(
                    'You specified a vpc_id that does not exist.',
                    vpc_id
                )

            super(Vpc, self).__init__(name=name)

            # Check that ipv4 is a valid network range or set the default value
            if ipv4_cidr:
                try:
                    self._ipv4_cidr = str(ipaddress.IPv4Network(
                        six.text_type(ipv4_cidr)
                    ))
                except (ipaddress.AddressValueError, ValueError):
                    raise ValueError(
                        'If provided, ipv4_cidr must be a valid IPv4 network '
                        'range.'
                    )
            else:
                self._ipv4_cidr = str(ipaddress.IPv4Network(u'10.0.0.0/16'))

            if instance_tenancy:
                if instance_tenancy in ('default', 'dedicated'):
                    self._instance_tenancy = instance_tenancy
                else:
                    raise ValueError(
                        'If provided, instance tenancy must be '
                        'one of ("default", "dedicated").'
                    )
            else:
                self._instance_tenancy = 'default'

            self._vpc_id = self._create()
            self._subnet_ids = self._add_subnets()

    # Declare read-only properties
    pre_existing = property(operator.attrgetter('_pre_existing'))
    ipv4_cidr = property(operator.attrgetter('_ipv4_cidr'))
    instance_tenancy = property(operator.attrgetter('_instance_tenancy'))
    vpc_id = property(operator.attrgetter('_vpc_id'))
    subnet_ids = property(operator.attrgetter('_subnet_ids'))

    def _exists_already(self, vpc_id, name):
        """Check if an AWS VPC exists already

        If VPC exists, return namedtuple with VPC info. Otherwise, set the
        namedtuple's `exists` field to `False`. The remaining fields default
        to `None`.

        Returns
        -------
        namedtuple RoleExists
            A namedtuple with fields ['exists', 'name', 'ipv4_cidr',
            'instance_tenancy', 'vpc_id', 'subnet_ids']
        """
        # define a namedtuple for return value type
        ResourceExists = namedtuple(
            'ResourceExists',
            ['exists', 'name', 'ipv4_cidr', 'instance_tenancy',
             'vpc_id', 'subnet_ids']
        )
        # make all but the first value default to None
        ResourceExists.__new__.__defaults__ = \
            (None,) * (len(ResourceExists._fields) - 1)

        if vpc_id:
            try:
                # If user supplied vpc_id, check that
                response = EC2.describe_vpcs(VpcIds=[vpc_id])

                # Save vpcs for outside the if/else block
                vpcs = response.get('Vpcs')
            except EC2.exceptions.ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code == 'InvalidVpcID.NotFound':
                    # VPC doesn't exist
                    # Save vpcs for outside the if/else block
                    vpcs = None
                else:  # pragma: nocover
                    # I can't think of a test case for this
                    # But we should pass through any unexpected errors
                    raise e
        else:
            # Else check for the tag "Name: name"
            response = EC2.describe_tags(
                Filters=[
                    {
                        'Name': 'resource-type',
                        'Values': ['vpc']
                    },
                    {
                        'Name': 'key',
                        'Values': ['Name']
                    },
                    {
                        'Name': 'value',
                        'Values': [name]
                    }
                ]
            )

            if response.get('Tags'):
                vpc_id = response.get('Tags')[0]['ResourceId']
                response = EC2.describe_vpcs(VpcIds=[vpc_id])
                vpcs = response.get('Vpcs')
            else:
                vpcs = None

        if vpcs:
            vpc = vpcs[0]
            try:
                tags = vpc['Tags']
            except KeyError:
                tags = []
            ipv4_cidr = vpc['CidrBlock']
            vpc_id = vpc['VpcId']
            instance_tenancy = vpc['InstanceTenancy']

            # Find the name tag
            try:
                name_tag = list(filter(lambda d: d['Key'] == 'Name', tags))[0]
                name = name_tag['Value']
            except IndexError:
                name = 'cloudknot-acquired-pre-existing-vpc'
                EC2.create_tags(
                    Resources=[vpc_id],
                    Tags=[
                        {
                            'Key': 'owner',
                            'Value': 'cloudknot'
                        },
                        {
                            'Key': 'Name',
                            'Value': name
                        }
                    ]
                )

            response = EC2.describe_subnets(
                Filters=[
                    {
                        'Name': 'vpc-id',
                        'Values': [vpc_id]
                    }
                ]
            )

            subnet_ids = [d['SubnetId'] for d in response.get('Subnets')]

            logging.info('VPC {vpcid:s} already exists.'.format(vpcid=vpc_id))

            return ResourceExists(
                exists=True, name=name, ipv4_cidr=ipv4_cidr,
                instance_tenancy=instance_tenancy, vpc_id=vpc_id,
                subnet_ids=subnet_ids
            )
        else:
            return ResourceExists(exists=False)

    def _create(self):
        """Create AWS virtual private cloud (VPC) using instance parameters

        Returns
        -------
        string
            VPC-ID for the created VPC
        """
        response = EC2.create_vpc(
            CidrBlock=self.ipv4_cidr,
            InstanceTenancy=self.instance_tenancy
        )

        vpc_id = response.get('Vpc')['VpcId']

        logging.info('Created VPC {vpcid:s}.'.format(vpcid=vpc_id))

        # Wait for VPC to exist and be available
        wait_for_vpc = EC2.get_waiter('vpc_exists')
        wait_for_vpc.wait(VpcIds=[vpc_id])
        wait_for_vpc = EC2.get_waiter('vpc_available')
        wait_for_vpc.wait(VpcIds=[vpc_id])
        # Tag the VPC with name and owner
        EC2.create_tags(
            Resources=[vpc_id],
            Tags=[
                {
                    'Key': 'owner',
                    'Value': 'cloudknot'
                },
                {
                    'Key': 'Name',
                    'Value': self.name
                }
            ]
        )

        # Add this VPC to the list of VPCs in the config file
        cloudknot.config.add_resource('vpc', vpc_id, self.name)

        return vpc_id

    def _add_subnets(self):
        """Add one subnet to this VPC for each availability zone

        Returns
        -------
        None
        """
        # Add a subnet for each availability zone
        response = EC2.describe_availability_zones()
        zones = response.get('AvailabilityZones')

        # Get an IPv4Network instance representing the VPC CIDR block
        cidr = ipaddress.IPv4Network(six.text_type(self.ipv4_cidr))

        # Ensure that the CIDR block has enough addresses to cover each zone
        if cidr.num_addresses < len(zones):  # pragma: nocover
            raise ValueError('IPv4 CIDR block does not have enough addresses '
                             'for each availability zone')

        # Each increment of prefixlen_diff will give us another power of 2
        # of subnets. So prefixlen_diff should be the log2 of the number of
        # subnets we want (i.e. the number of zones)
        try:
            prefixlen_diff = ceil(log2(len(zones)))
        except NameError:  # pragma: nocover
            # python 2.7 compatibility
            prefixlen_diff = int(ceil(log(len(zones), 2)))

        # Get list of subnet CIDR blocks truncating list to len(zones)
        subnet_ipv4_cidrs = list(cidr.subnets(
            prefixlen_diff=prefixlen_diff
        ))[:len(zones)]

        subnet_ids = []

        for zone, subnet_cidr in zip(zones, subnet_ipv4_cidrs):
            # Create a subnet for each zone
            response = EC2.create_subnet(
                AvailabilityZone=zone['ZoneName'],
                CidrBlock=str(subnet_cidr),
                VpcId=self.vpc_id
            )

            subnet_id = response.get('Subnet')['SubnetId']
            subnet_ids.append(subnet_id)

            logging.info('Created subnet {id:s}.'.format(id=subnet_id))

        # Tag all subnets with name and owner
        wait_for_subnet = EC2.get_waiter('subnet_available')
        retry = tenacity.Retrying(
            wait=tenacity.wait_exponential(max=60),
            stop=tenacity.stop_after_delay(300),
            retry=tenacity.retry_if_exception_type(
                botocore.exceptions.WaiterError
            )
        )
        retry.call(wait_for_subnet.wait, SubnetIds=subnet_ids)
        EC2.create_tags(
            Resources=subnet_ids,
            Tags=[
                {
                    'Key': 'owner',
                    'Value': 'cloudknot'
                },
                {
                    'Key': 'vpc-name',
                    'Value': self.name
                }
            ]
        )

        return subnet_ids

    def clobber(self):
        """Delete this AWS virtual private cloud (VPC)

        Returns
        -------
        None
        """
        try:
            # Delete the subnets
            for subnet_id in self.subnet_ids:
                EC2.delete_subnet(SubnetId=subnet_id)
                logging.info('Deleted subnet {id:s}'.format(id=subnet_id))

            # Delete the VPC
            EC2.delete_vpc(VpcId=self.vpc_id)

            # Remove this VPC from the list of VPCs in the config file
            cloudknot.config.remove_resource('vpc', self.vpc_id)

            logging.info('Deleted VPC {name:s}'.format(name=self.name))
        except EC2.exceptions.ClientError as e:
            # Check for dependency violation and pass exception to user
            error_code = e.response['Error']['Code']
            if error_code == 'DependencyViolation':
                response = EC2.describe_security_groups(
                    Filters=[{
                        'Name': 'vpc-id',
                        'Values': [self.vpc_id]
                    }]
                )

                ids = [sg['GroupId'] for sg in response.get('SecurityGroups')]
                raise CannotDeleteResourceException(
                    'Could not delete this VPC because it has dependencies. '
                    'It may have security groups associated with it. If you '
                    'still want to delete this VPC, you should first delete '
                    'the security groups with the following IDs '
                    '{sg_ids:s}'.format(sg_ids=str(ids)),
                    resource_id=ids
                )
            else:  # pragma: nocover
                # I can't think of a test case to make this happen
                raise e


# noinspection PyPropertyAccess,PyAttributeOutsideInit
class SecurityGroup(NamedObject):
    """Class for defining an AWS Security Group"""
    def __init__(self, security_group_id=None, name=None, vpc=None,
                 description=None):
        """Initialize an AWS Security Group.

        Parameters
        ----------
        security_group_id : string
            ID of the security group to be retrieved

        name : string
            Name of the security group to be created

        vpc : Vpc
            Amazon virtual private cloud in which to establish this
            security group

        description : string
            description of this security group
            if description == None (default), then description is set to
            "This security group was generated by cloudknot"
            Default: None
        """
        # User must specify either an ID or a name and VPC
        if not (security_group_id or (name and vpc)):
            raise ValueError(
                'You must specify either a security group id for an existing '
                'security group or a name and VPC for a new security group.'
            )

        # Check that user didn't over-specify input
        if security_group_id and any([name, vpc, description]):
            raise ValueError(
                'You must specify either a security group id for an existing '
                'security group or input parameters for a new security group. '
                'You cannot do both.'
            )

        # Validate VPC input
        if vpc and not isinstance(vpc, Vpc):
            raise ValueError('If provided, vpc must be an instance of Vpc.')

        vpc_id = vpc.vpc_id if vpc else None

        resource = self._exists_already(security_group_id, name, vpc_id)
        self._pre_existing = resource.exists

        if resource.exists:
            super(SecurityGroup, self).__init__(name=resource.name)

            self._vpc = None
            self._vpc_id = resource.vpc_id
            self._description = resource.description
            self._security_group_id = resource.security_group_id

            if name or vpc:
                raise ResourceExistsException(
                    'The security group name {name:s} is already in use for '
                    'VPC {vpc_id:s}. If you would like to retrieve this '
                    'security group, try SecurityGroup(security_group_id='
                    '{sg_id:s}).'.format(
                        name=self.name, vpc_id=self.vpc_id,
                        sg_id=self.security_group_id
                    ),
                    resource_id=self.security_group_id
                )

            cloudknot.config.add_resource(
                'security-groups', self.security_group_id, self.name
            )

            logging.info('Retrieved pre-existing security group {id:s}'.format(
                id=self.security_group_id
            ))
        else:
            if security_group_id:
                raise ResourceDoesNotExistException(
                    'The security group ID that you provided does not exist.',
                    resource_id=security_group_id
                )

            super(SecurityGroup, self).__init__(name=name)

            self._vpc = vpc
            self._vpc_id = vpc.vpc_id
            self._description = str(description) if description else \
                'This security group was automatically generated by cloudknot.'
            self._security_group_id = self._create()

    # Declare read-only properties
    pre_existing = property(operator.attrgetter('_pre_existing'))
    vpc = property(operator.attrgetter('_vpc'))
    vpc_id = property(operator.attrgetter('_vpc_id'))
    description = property(operator.attrgetter('_description'))
    security_group_id = property(operator.attrgetter('_security_group_id'))

    def _exists_already(self, security_group_id, name, vpc_id):
        """Check if an AWS security group exists already

        If security group exists, return namedtuple with security group info.
        Otherwise, set the namedtuple's `exists` field to `False`. The
        remaining fields default to `None`.

        Returns
        -------
        namedtuple RoleExists
            A namedtuple with fields
            ['exists', 'name', 'vpc_id', 'description', 'security_group_id']
        """
        # define a namedtuple for return value type
        ResourceExists = namedtuple(
            'ResourceExists',
            ['exists', 'name', 'vpc_id', 'description', 'security_group_id']
        )
        # make all but the first value default to None
        ResourceExists.__new__.__defaults__ = \
            (None,) * (len(ResourceExists._fields) - 1)

        if security_group_id:
            try:
                response = EC2.describe_security_groups(
                    GroupIds=[security_group_id]
                )
            except EC2.exceptions.ClientError as e:
                # If the group_id doesn't exist or isn't formatted correctly,
                # return exists=False
                if e.response.get('Error')['Code'] in [
                    'InvalidGroup.NotFound', 'InvalidGroupId.Malformed'
                ]:
                    return ResourceExists(exists=False)
                else:  # pragma: no cover
                    # I could not think of a unit test case where the
                    # describe_security_groups request would yield a different
                    # error, but one should still pass through unhandled
                    # errors even if (especially if) one can't think of what
                    # they'll be.
                    raise e
        else:
            response = EC2.describe_security_groups(
                Filters=[
                    {
                        'Name': 'group-name',
                        'Values': [name]
                    },
                    {
                        'Name': 'vpc-id',
                        'Values': [vpc_id]
                    }
                ]
            )

        sg = response.get('SecurityGroups')
        if sg:
            name = sg[0]['GroupName']
            vpc_id = sg[0]['VpcId']
            description = sg[0]['Description']
            group_id = sg[0]['GroupId']
            return ResourceExists(
                exists=True, name=name, vpc_id=vpc_id, description=description,
                security_group_id=group_id
            )
        else:
            return ResourceExists(exists=False)

    def _create(self):
        """Create AWS security group using instance parameters

        Returns
        -------
        string
            security group ID for the created security group
        """
        # Create the security group
        response = EC2.create_security_group(
            GroupName=self.name,
            Description=self.description,
            VpcId=self.vpc.vpc_id
        )

        group_id = response.get('GroupId')

        # Add ingress rules to the security group
        ipv4_ranges = [{
            'CidrIp': '0.0.0.0/0'
        }]

        ipv6_ranges = [{
            'CidrIpv6': '::/0'
        }]

        ip_permissions = [{
            'IpProtocol': 'TCP',
            'FromPort': 80,
            'ToPort': 80,
            'IpRanges': ipv4_ranges,
            'Ipv6Ranges': ipv6_ranges
        }, {
            'IpProtocol': 'TCP',
            'FromPort': 22,
            'ToPort': 22,
            'IpRanges': ipv4_ranges,
            'Ipv6Ranges': ipv6_ranges
        }]

        retry = tenacity.Retrying(
            wait=tenacity.wait_exponential(max=60),
            stop=tenacity.stop_after_delay(300),
            retry=tenacity.retry_if_exception_type(
                EC2.exceptions.ClientErro
            )
        )
        retry.call(
            EC2.authorize_security_group_ingress,
            GroupId=group_id,
            IpPermissions=ip_permissions
        )

        logging.info('Created security group {id:s}'.format(id=group_id))

        # Tag the security group with owner=cloudknot
        retry.call(
            EC2.create_tags,
            Resources=[group_id],
            Tags=[
                {
                    'Key': 'owner',
                    'Value': 'cloudknot'
                }
            ]
        )

        # Add this security group to the list of security groups in the
        # config file
        cloudknot.config.add_resource('security-groups', group_id, self.name)

        return group_id

    def clobber(self):
        """Delete this AWS security group and associated resources

        Returns
        -------
        None
        """
        # Get dependent EC2 instances
        response = EC2.describe_instances(Filters=[{
            'Name': 'vpc-id',
            'Values': [self.vpc_id]
        }])

        def has_security_group(instance, sg_id):
            return sg_id in [d['GroupId'] for d in instance['SecurityGroups']]

        deps = []
        for r in response.get('Reservations'):
            deps = deps + [i['InstanceId'] for i in r['Instances']
                           if has_security_group(i, self.security_group_id)]

        # Delete the dependent instances
        if deps:
            EC2.terminate_instances(InstanceIds=deps)
            logging.info('Deleted dependent EC2 instances: {deps:s}'.format(
                deps=str(deps)
            ))

        # Get dependent compute environments
        response = BATCH.describe_compute_environments()
        ce_names = [
            ce['computeEnvironmentName'] for ce
            in response.get('computeEnvironments')
            if self.security_group_id
            in ce['computeResources']['securityGroupIds']
        ]
        ce_arns = [
            ce['computeEnvironmentArn'] for ce
            in response.get('computeEnvironments')
            if self.security_group_id
            in ce['computeResources']['securityGroupIds']
        ]

        # Wait for them to be updated / deleted
        for name, arn in zip(ce_names, ce_arns):
            wait_for_compute_environment(arn=arn, name=name)

        # Delete the security group
        try:
            EC2.delete_security_group(GroupId=self.security_group_id)
        except EC2.exceptions.ClientError as e:  # pragma: nocover
            error_code = e.response['Error']['Code']
            if error_code == 'DependencyViolation':
                time.sleep(30)
                EC2.delete_security_group(GroupId=self.security_group_id)
            else:
                raise e

        # Remove this VPC from the list of VPCs in the config file
        cloudknot.config.remove_resource(
            'security-groups', self.security_group_id
        )

        logging.info('Clobbered security group {id:s}'.format(
            id=self.security_group_id
        ))
