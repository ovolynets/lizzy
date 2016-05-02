from typing import Optional, List  # noqa pylint: disable=unused-import

import logging
import connexion
import yaml
from decorator import decorator
from copy import deepcopy

from lizzy import config
from lizzy.apps.kio import Kio
from lizzy.apps.senza import Senza
from lizzy.exceptions import (ObjectNotFound, ExecutionError, SenzaRenderError,
                              TrafficNotUpdated, SenzaDomainsError,
                              SenzaTrafficError)
from lizzy.models.stack import Stack
from lizzy.security import bouncer
from lizzy.util import filter_empty_values, timestamp_to_uct
from lizzy.version import VERSION

logger = logging.getLogger('lizzy.api')  # pylint: disable=invalid-name


def _make_headers() -> dict:
    return {'X-Lizzy-Version': VERSION}


def _make_stack_api_compliant(stack: dict):
    stack = deepcopy(stack)  # avoid bugs

    # Return time according to
    # http://zalando.github.io/restful-api-guidelines/data-formats/DataFormats.html#must-use-standard-date-and-time-formats
    creation_date = timestamp_to_uct(stack['creation_time'])
    stack['creation_time'] = '{:%FT%T%z}'.format(creation_date)

    # TODO check if all and only the parameters in the api are given

    return stack


@decorator
def exception_to_connexion_problem(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except ObjectNotFound as exception:
        problem = connexion.problem(404, 'Not Found',
                                    "Stack not found: {}".format(exception.uid),
                                    headers=_make_headers())
        return problem


@bouncer
def all_stacks() -> dict:
    """
    GET /stacks/
    """
    stacks = Stack.list()
    stacks.sort(key=lambda stack: stack['creation_time'])
    return stacks, 200, _make_headers()


@bouncer
def create_stack(new_stack: dict) -> dict:
    """
    POST /stacks/

    :param new_stack: New stack
    """

    keep_stacks = new_stack['keep_stacks']  # type: int
    new_traffic = new_stack['new_traffic']  # type: int
    image_version = new_stack['image_version']  # type: str
    application_version = new_stack.get('application_version')  # type: Optional[str]
    stack_version = new_stack['stack_version']  # type: str
    senza_yaml = new_stack['senza_yaml']  # type: str
    parameters = new_stack.get('parameters', [])
    disable_rollback = new_stack.get('disable_rollback', False)
    artifact_name = None

    senza = Senza(config.region)

    try:
        cf_raw_definition = senza.render_definition(senza_yaml,
                                                    stack_version,
                                                    application_version,
                                                    parameters)
    except SenzaRenderError as exception:
        return connexion.problem(400,
                                 'Invalid senza yaml',
                                 exception.message,
                                 headers=_make_headers())

    try:
        stack_name = cf_raw_definition['Mappings']['Senza']['Info']['StackName']

        for resource, definition in cf_raw_definition['Resources'].items():
            if definition['Type'] == 'AWS::AutoScaling::LaunchConfiguration':
                taupage_yaml = definition['Properties']['UserData']['Fn::Base64']
                taupage_config = yaml.safe_load(taupage_yaml)
                artifact_name = taupage_config['source']

        if artifact_name is None:
            missing_component_error = "Missing component type Senza::TaupageAutoScalingGroup"
            problem = connexion.problem(400,
                                        'Invalid senza yaml',
                                        missing_component_error,
                                        headers=_make_headers())

            logger.error(missing_component_error, extra={
                'cf_definition': repr(cf_raw_definition)})
            return problem

    except KeyError as exception:
        logger.error("Couldn't get stack name from definition.",
                     extra={'cf_definition': repr(cf_raw_definition)})
        missing_property = str(exception)
        problem = connexion.problem(400,
                                    'Invalid senza yaml',
                                    "Missing property in senza yaml: {}".format(missing_property),
                                    headers=_make_headers())
        return problem

    # Create the Stack
    logger.info("Creating stack %s...", stack_name)

    if application_version:
        kio_extra = {'stack_name': stack_name, 'version': application_version}
        logger.info("Registering version on kio...", extra=kio_extra)
        kio = Kio()
        if kio.versions_create(application_id=stack_name,
                               version=application_version,
                               artifact=artifact_name):
            logger.info("Version registered in Kio.", extra=kio_extra)
        else:
            logger.error("Error registering version in Kio.", extra=kio_extra)

    senza = Senza(config.region)
    stack_extra = {'stack_name': stack_name,
                   'stack_version': stack_version,
                   'image_version': image_version,
                   'parameters': parameters}
    tags = {'LizzyKeepStacks': keep_stacks,
            'LizzyTargetTraffic': new_traffic}
    if senza.create(senza_yaml, stack_version, image_version, parameters,
                    disable_rollback, tags):
        logger.info("Stack created.", extra=stack_extra)
        # Mark the stack as CREATE_IN_PROGRESS. Even if this isn't true anymore
        # this will be handled in the job anyway
        stack_dict = Stack.get(stack_name, stack_version)
        return stack_dict, 201, _make_headers()
    else:
        logger.error("Error creating stack.", extra=stack_extra)
        return connexion.problem(400, 'Deployment Failed',
                                 "Senza create command failed.",
                                 headers=_make_headers())


@bouncer
@exception_to_connexion_problem
def get_stack(stack_id: str) -> dict:
    """
    GET /stacks/{id}
    """
    stack_name, stack_version = stack_id.rsplit('-', 1)
    stack_dict = Stack.get(stack_name, stack_version)
    return stack_dict, 200, _make_headers()


@bouncer
@exception_to_connexion_problem
def patch_stack(stack_id: str, stack_patch: dict) -> dict:
    """
    PATCH /stacks/{id}

    Update traffic and Taupage image
    """
    stack_patch = filter_empty_values(stack_patch)

    stack_name, stack_version = stack_id.rsplit('-', 1)
    stack_dict = Stack.get(stack_name, stack_version)
    senza = Senza(config.region)
    log_info = {'stack_id': stack_id,
                'stack_name': stack_name}

    if 'new_ami_image' in stack_patch:
        # Change the AMI image of the Auto Scaling Group (ASG) and respawn the
        # instances to use new image.
        new_ami_image = stack_patch['new_ami_image']
        try:
            senza.patch(stack_name, stack_version, new_ami_image)
            senza.respawn_instances(stack_name, stack_version)
        except ExecutionError as exception:
            logger.info(exception.message, extra=log_info)
            return connexion.problem(400, 'Image update failed',
                                     exception.message,
                                     headers=_make_headers())

    if 'new_traffic' in stack_patch:
        new_traffic = stack_patch['new_traffic']
        try:
            domains = senza.domains(stack_name)
            if domains:
                logger.info("Switching app traffic to stack.",
                            extra=log_info)
                senza.traffic(stack_name=stack_name,
                              stack_version=stack_version,
                              percentage=new_traffic)
            else:
                logger.info("App does not have a domain so traffic will not be switched.",
                            extra=log_info)
                raise TrafficNotUpdated("App does not have a domain.")
        except SenzaDomainsError as exception:
            logger.exception(
                "Failed to get domains. Traffic will not be switched.",
                extra=log_info)
            return connexion.problem(400, 'Traffic update failed',
                                     exception.message,
                                     headers=_make_headers())
        except SenzaTrafficError as exception:
            logger.exception("Failed to switch app traffic.", extra=log_info)
            return connexion.problem(400, 'Traffic update failed',
                                     exception.message,
                                     headers=_make_headers())

    # refresh the dict
    stack_dict = Stack.get(stack_name, stack_version)

    return stack_dict, 202, _make_headers()


@bouncer
def delete_stack(stack_id: str) -> dict:
    """
    DELETE /stacks/{id}

    Delete a stack
    """
    stack_name, stack_version = stack_id.rsplit('-', 1)
    senza = Senza(config.region)

    logger.info("Removing stack %s...", stack_id)

    try:
        senza.remove(stack_name, stack_version)
        logger.info("Stack %s removed.", stack_id)
    except ExecutionError as exception:
        logger.exception("Failed to remove stack %s.", stack_id)
        return connexion.problem(500, 'Stack deletion failed',
                                 exception.output,
                                 headers=_make_headers())
    else:
        return '', 204, _make_headers()


def not_found_path_handler(error):
    return connexion.problem(401, 'Unauthorized', '')
