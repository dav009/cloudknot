from __future__ import absolute_import, division, print_function

import cloudknot.config
import json
import logging
import six
import tenacity
from collections import namedtuple

from .base_classes import ObjectWithArn, clients, \
    ResourceExistsException, ResourceDoesNotExistException, \
    ResourceClobberedException, CannotDeleteResourceException

__all__ = ["IamRole"]

mod_logger = logging.getLogger(__name__)


# noinspection PyPropertyAccess,PyAttributeOutsideInit
class IamRole(ObjectWithArn):
    """Class for defining AWS IAM Roles"""
    def __init__(self, name, description=None, service=None,
                 policies=(), add_instance_profile=False):
        """Initialize an AWS IAM Role object.

        Parameters
        ----------
        name : string
            Name of the IAM role

        description : string
            description of this IAM role
            If description == None (default), then it is reset to
            "This role was generated by cloudknot"
            Default: None

        service : {'ecs-tasks', 'batch', 'ec2', 'lambda', 'spotfleet'}
            service role on which this AWS IAM role should be based.
            Default: 'ecs-tasks'

        policies : tuple of strings
            tuple of names of AWS policies to attach to this role
            Default: ()

        add_instance_profile : boolean
            flag to create an AWS instance profile and attach this role to it
            Default: False
        """
        super(IamRole, self).__init__(name=name)
        self._region = 'global'

        role = self._exists_already()
        self._pre_existing = role.exists

        if role.exists:
            if any([description, policies, add_instance_profile, service]):
                raise ResourceExistsException(
                    'You specified parameters for a role that already exists. '
                    'If you would like to instantiate a role with these '
                    'parameters, choose a different name. If you would like '
                    'to retrieve the parameters associated with this '
                    'pre-existing role, leave all other input blank.',
                    resource_id=self.name
                )

            self._description = role.description
            self._role_policy_document = role.role_policy_document
            rpd_statement = role.role_policy_document['Statement'][0]
            self._service = rpd_statement['Principal']['Service']
            self._policies = role.policies
            self._arn = role.arn
            self._section_name = self._get_section_name('roles')
            cloudknot.config.add_resource(self._section_name,
                                          self.name, self.arn)
        else:
            if not any([description, service, policies]):
                raise ResourceDoesNotExistException(
                    'IAM Role {name:s} does not exist and you provided no '
                    'input parameters with which to create it.'.format(
                        name=name
                    ),
                    resource_id=name
                )

            if description:
                self._description = str(description)
            else:
                self._description = 'This role was generated by cloudknot'

            service = service if service else 'ecs-tasks'
            if service in self._allowed_services:
                self._service = service + '.amazonaws.com'
            else:
                msg = 'service must be in ' + str(self._allowed_services)
                raise ValueError(msg)

            # "Version": "2012-10-17",
            role_policy = {
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {
                        "Service": self._service
                    },
                    "Action": "sts:AssumeRole"
                }]
            }
            self._role_policy_document = role_policy

            # Check the user supplied policies against the available policies
            # Remove redundant entries
            if isinstance(policies, six.string_types):
                input_policies = {policies}
            else:
                try:
                    if all(isinstance(x, six.string_types) for x in policies):
                        input_policies = set(list(policies))
                    else:
                        raise ValueError('policies must be a string or a '
                                         'sequence of strings.')
                except TypeError:
                    raise ValueError('policies must be a string or a '
                                     'sequence of strings')

            # Get all AWS policies
            response = clients['iam'].list_policies()
            aws_policies = [d['PolicyName'] for d in response.get('Policies')]

            # If results are paginated, continue appending to aws_policies,
            # using `Marker` to tell next call where to start
            while response['IsTruncated']:
                response = clients['iam'].list_policies(
                    Marker=response['Marker']
                )
                aws_policies += [d['PolicyName'] for d
                                 in response.get('Policies')]

            # If input policies are not a subset of aws_policies, throw error
            if not (input_policies < set(aws_policies)):
                bad_policies = input_policies - set(aws_policies)
                raise ValueError(
                    'Could not find the policies {bad_policies!s} on '
                    'AWS.'.format(bad_policies=bad_policies)
                )

            self._policies = tuple(input_policies)

            if not isinstance(add_instance_profile, bool):
                raise ValueError('add_instance_profile is a boolean input')

            self._arn = self._create(add_instance_profile=add_instance_profile)

    _allowed_services = ['batch', 'ec2', 'ecs-tasks', 'lambda', 'spotfleet']

    # Declare read-only properties
    @property
    def pre_existing(self):
        """Boolean flag to indicate whether this resource was pre-existing

        True if resource was retrieved from AWS,
        False if it was created on __init__.
        """
        return self._pre_existing

    @property
    def description(self):
        """Description of this IAM role"""
        return self._description

    @property
    def service(self):
        """Service role on which this AWS IAM role is based.

        `service` will be one of
        ['ecs-tasks', 'batch', 'ec2', 'lambda', 'spotfleet']
        """
        return self._service

    @property
    def policies(self):
        """Tuple of names of AWS policies attached to this role"""
        return self._policies

    @property
    def role_policy_document(self):
        """Role policy document for this IAM role"""
        return self._role_policy_document

    def _exists_already(self):
        """Check if an IAM Role exists already

        If role exists, return namedtuple with role info. Otherwise, set the
        namedtuple's `exists` field to `False`. The remaining fields default
        to `None`.

        Returns
        -------
        namedtuple RoleExists
            A namedtuple with fields ['exists', 'description',
            'role_policy_document', 'policies', 'arn']
        """
        # define a namedtuple for return value type
        RoleExists = namedtuple(
            'RoleExists',
            ['exists', 'description', 'role_policy_document', 'policies',
             'arn']
        )
        # make all but the first value default to None
        RoleExists.__new__.__defaults__ = \
            (None,) * (len(RoleExists._fields) - 1)

        try:
            # If role exists, retrieve info
            retry = tenacity.Retrying(
                wait=tenacity.wait_exponential(max=4),
                stop=tenacity.stop_after_delay(5),
                retry=tenacity.retry_if_exception_type(
                    clients['iam'].exceptions.NoSuchEntityException
                )
            )
            response = retry.call(clients['iam'].get_role, RoleName=self.name)
            arn = response.get('Role')['Arn']
            role_policy = response.get('Role')['AssumeRolePolicyDocument']
            try:
                description = response.get('Role')['Description']
            except KeyError:
                description = ''

            response = clients['iam'].list_attached_role_policies(
                RoleName=self.name
            )
            attached_policies = response.get('AttachedPolicies')
            policies = tuple([d['PolicyName'] for d in attached_policies])

            mod_logger.info('IAM role {name:s} already exists: {arn:s}'.format(
                name=self.name, arn=arn
            ))

            return RoleExists(
                exists=True, description=description,
                role_policy_document=role_policy, policies=policies, arn=arn
            )
        except tenacity.RetryError as e:
            try:
                e.reraise()
            except clients['iam'].exceptions.NoSuchEntityException:
                return RoleExists(exists=False)

    def _create(self, add_instance_profile=False):
        """Create AWS IAM role using instance parameters

        Returns
        -------
        string
            Amazon Resource Number (ARN) for the created IAM role
        """
        response = clients['iam'].create_role(
            RoleName=self.name,
            AssumeRolePolicyDocument=json.dumps(self.role_policy_document),
            Description=self.description
        )
        role_arn = response.get('Role')['Arn']

        mod_logger.info('Created role {name:s} with arn {arn:s}'.format(
            name=self.name, arn=role_arn
        ))

        for policy in self.policies:
            # Get the corresponding arn for each input policy
            policy_response = clients['iam'].list_policies()
            policy_filter = list(filter(
                lambda p: p['PolicyName'] == policy,
                policy_response.get('Policies')
            ))

            # If policy_filter is empty, it is because the list_policies
            # results are paginated, use `Marker` to get the next page
            while not len(policy_filter):
                policy_response = clients['iam'].list_policies(
                    Marker=policy_response['Marker']
                )

                policy_filter = list(filter(
                    lambda p: p['PolicyName'] == policy,
                    policy_response.get('Policies')
                ))

            policy_arn = policy_filter[0]['Arn']

            retry = tenacity.Retrying(
                wait=tenacity.wait_exponential(max=4),
                stop=tenacity.stop_after_delay(5),
                retry=tenacity.retry_if_exception_type(
                    clients['iam'].exceptions.NoSuchEntityException
                )
            )
            retry.call(
                clients['iam'].attach_role_policy,
                PolicyArn=policy_arn, RoleName=self.name
            )

            mod_logger.info(
                'Attached policy {policy:s} to role {role:s}'.format(
                    policy=policy, role=self.name
                )
            )

        if add_instance_profile:
            try:
                # Create the instance profile
                clients['iam'].create_instance_profile(
                    InstanceProfileName=self.name
                )

                # Wait for it to show up
                wait_for_instance_profile = clients['iam'].get_waiter(
                    'instance_profile_exists'
                )

                wait_for_instance_profile.wait(
                    InstanceProfileName=self.name
                )

                # Add to role
                clients['iam'].add_role_to_instance_profile(
                    InstanceProfileName=self.name,
                    RoleName=self.name
                )
            except clients['iam'].exceptions.EntityAlreadyExistsException:
                # Instance profile already exists, just add to role
                clients['iam'].add_role_to_instance_profile(
                    InstanceProfileName=self.name,
                    RoleName=self.name
                )

            mod_logger.info('Created instance profile {name:s}'.format(
                name=self.name
            ))

        # Add this role to the list of roles in the config file
        self._section_name = self._get_section_name('roles')
        cloudknot.config.add_resource(self._section_name, self.name, role_arn)

        return role_arn

    @property
    def instance_profile_arn(self):
        """Return ARN for attached instance profile if applicable

        Returns
        -------
        arn : string or None
            ARN for attached instance profile if any, otherwise None
        """
        if self.clobbered:
            raise ResourceClobberedException(
                'This batch job has already been clobbered.',
                self.arn
            )

        self.check_profile()

        response = clients['iam'].list_instance_profiles_for_role(
            RoleName=self.name
        )

        if response.get('InstanceProfiles'):
            # This role has instance profiles, return the first
            arn = response.get('InstanceProfiles')[0]['Arn']
            return arn
        else:
            # This role has no instance profiles, return None
            return None

    def clobber(self):
        """Delete this AWS IAM role and remove from config file"""
        if self.clobbered:
            return

        self.check_profile()

        if self.service == 'batch.amazonaws.com':
            # If this is a batch service role, wait for any dependent compute
            # environments to finish deleting In order to prevent INVALID
            # compute environment as described in
            # docs.aws.amazon.com/batch/latest/userguide/troubleshooting.html
            response = clients['batch'].describe_compute_environments()
            dependent_ces = [c for c in response.get('computeEnvironments')
                             if c['serviceRole'] == self.arn]

            conflicting_ces = [c for c in dependent_ces
                               if c['status'] not in ['DELETING', 'DELETED']]

            if conflicting_ces:
                raise CannotDeleteResourceException(
                    'Could not delete this batch service role because it '
                    'has compute environments associated with it that are '
                    'not being deleted. If you want to delete this role, '
                    'first delete the following compute environments: '
                    '{ces!s}'.format(ces=conflicting_ces),
                    resource_id=conflicting_ces
                )

            def is_deleting(res):
                ce = res.get('computeEnvironments')
                if ce:
                    return ce[0]['status'] == 'DELETING'
                else:
                    return False

            retry = tenacity.Retrying(
                wait=tenacity.wait_exponential(max=16),
                stop=tenacity.stop_after_delay(120),
                retry=tenacity.retry_if_result(is_deleting)
            )

            for ce in dependent_ces:
                try:
                    response = retry.call(
                        clients['batch'].describe_compute_environments,
                        computeEnvironments=[ce['computeEnvironmentArn']]
                    )
                except tenacity.RetryError:  # pragma: nocover
                    raise CannotDeleteResourceException(
                        'Could not delete this batch service role because it '
                        'it is taking too long for a dependent compute '
                        'environment to be deleted. If you want to delete '
                        'this role, first delete the compute environment '
                        '{arn:s}'.format(arn=ce['computeEnvironmentArn']),
                        resource_id=ce['computeEnvironmentArn']
                    )

        if self.instance_profile_arn:
            # Remove any instance profiles associated with this role
            response = clients['iam'].list_instance_profiles_for_role(
                RoleName=self.name
            )

            instance_profile_name = response.get(
                'InstanceProfiles'
            )[0]['InstanceProfileName']

            clients['iam'].remove_role_from_instance_profile(
                InstanceProfileName=instance_profile_name,
                RoleName=self.name
            )

            clients['iam'].delete_instance_profile(
                InstanceProfileName=instance_profile_name
            )

        for policy in self.policies:
            # Get the corresponding arn for each input policy
            policy_response = clients['iam'].list_policies()
            policy_filter = list(filter(
                lambda p: p['PolicyName'] == policy,
                policy_response.get('Policies')
            ))

            # If policy_filter is empty, it is because the list_policies
            # results are paginated, use `Marker` to get the next page
            while not len(policy_filter):
                policy_response = clients['iam'].list_policies(
                    Marker=policy_response['Marker']
                )

                policy_filter = list(filter(
                    lambda p: p['PolicyName'] == policy,
                    policy_response.get('Policies')
                ))

            policy_arn = policy_filter[0]['Arn']

            # Detach the policy from this role
            clients['iam'].detach_role_policy(
                RoleName=self.name,
                PolicyArn=policy_arn
            )

        # Delete role from AWS
        clients['iam'].delete_role(RoleName=self.name)

        # Remove this role from the list of roles in the config file
        cloudknot.config.remove_resource(self._section_name, self.name)

        # Set the clobbered parameter to True,
        # preventing subsequent method calls
        self._clobbered = True

        mod_logger.info('Deleted role {name:s}'.format(name=self.name))
