from __future__ import annotations

import base64 as _base64
import logging as _logging
import subprocess
import time
from typing import Optional

import requests as _requests
from flyteidl.service import admin_pb2_grpc as _admin_service
from flyteidl.service import auth_pb2
from flyteidl.service import auth_pb2_grpc as auth_service
from google.protobuf.json_format import MessageToJson as _MessageToJson
from grpc import RpcError as _RpcError
from grpc import StatusCode as _GrpcStatusCode
from grpc import insecure_channel as _insecure_channel
from grpc import secure_channel as _secure_channel
from grpc import ssl_channel_credentials as _ssl_channel_credentials

from flytekit.clis.auth import credentials as _credentials_access
from flytekit.configuration import creds as creds_config
from flytekit.configuration.creds import CLIENT_CREDENTIALS_SECRET as _CREDENTIALS_SECRET
from flytekit.configuration.creds import CLIENT_ID as _CLIENT_ID
from flytekit.configuration.creds import COMMAND as _COMMAND
from flytekit.exceptions import user as _user_exceptions
from flytekit.exceptions.user import FlyteAuthenticationException
from flytekit.loggers import cli_logger


def _refresh_credentials_standard(flyte_client: RawSynchronousFlyteClient):
    """
    This function is used when the configuration value for AUTH_MODE is set to 'standard'.
    This either fetches the existing access token or initiates the flow to request a valid access token and store it.
    :param flyte_client: RawSynchronousFlyteClient
    :return:
    """
    authorization_header_key = flyte_client.public_client_config.authorization_metadata_key or None
    if not flyte_client.oauth2_metadata or not flyte_client.public_client_config:
        raise ValueError(
            "Raw Flyte client attempting client credentials flow but no response from Admin detected. "
            "Check your Admin server's .well-known endpoints to make sure they're working as expected."
        )
    client = _credentials_access.get_client(
        redirect_endpoint=flyte_client.public_client_config.redirect_uri,
        client_id=flyte_client.public_client_config.client_id,
        scopes=flyte_client.public_client_config.scopes,
        auth_endpoint=flyte_client.oauth2_metadata.authorization_endpoint,
        token_endpoint=flyte_client.oauth2_metadata.token_endpoint,
    )
    if client.has_valid_credentials and not flyte_client.check_access_token(client.credentials.access_token):
        # When Python starts up, if credentials have been stored in the keyring, then the AuthorizationClient
        # will have read them into its _credentials field, but it won't be in the RawSynchronousFlyteClient's
        # metadata field yet. Therefore, if there's a mismatch, copy it over.
        flyte_client.set_access_token(client.credentials.access_token, authorization_header_key)
        # However, after copying over credentials from the AuthorizationClient, we have to clear it to avoid the
        # scenario where the stored credentials in the keyring are expired. If that's the case, then we only try
        # them once (because client here is a singleton), and the next time, we'll do one of the two other conditions
        # below.
        client.clear()
        return
    elif client.can_refresh_token:
        client.refresh_access_token()
    else:
        client.start_authorization_flow()

    flyte_client.set_access_token(client.credentials.access_token, authorization_header_key)


def _refresh_credentials_basic(flyte_client: RawSynchronousFlyteClient):
    """
    This function is used by the _handle_rpc_error() decorator, depending on the AUTH_MODE config object. This handler
    is meant for SDK use-cases of auth (like pyflyte, or when users call SDK functions that require access to Admin,
    like when waiting for another workflow to complete from within a task). This function uses basic auth, which means
    the credentials for basic auth must be present from wherever this code is running.

    :param flyte_client: RawSynchronousFlyteClient
    :return:
    """
    if not flyte_client.oauth2_metadata or not flyte_client.public_client_config:
        raise ValueError(
            "Raw Flyte client attempting client credentials flow but no response from Admin detected. "
            "Check your Admin server's .well-known endpoints to make sure they're working as expected."
        )

    token_endpoint = flyte_client.oauth2_metadata.token_endpoint
    scopes = creds_config.SCOPES.get() or flyte_client.public_client_config.scopes
    scopes = ",".join(scopes)

    # Note that unlike the Pkce flow, the client ID does not come from Admin.
    client_secret = get_secret()
    cli_logger.debug("Basic authorization flow with client id {} scope {}".format(_CLIENT_ID.get(), scopes))
    authorization_header = get_basic_authorization_header(_CLIENT_ID.get(), client_secret)
    token, expires_in = get_token(token_endpoint, authorization_header, scopes)
    cli_logger.info("Retrieved new token, expires in {}".format(expires_in))
    authorization_header_key = flyte_client.public_client_config.authorization_metadata_key or None
    flyte_client.set_access_token(token, authorization_header_key)


def _refresh_credentials_from_command(flyte_client):
    """
    This function is used when the configuration value for AUTH_MODE is set to 'external_process'.
    It reads an id token generated by an external process started by running the 'command'.

    :param flyte_client: RawSynchronousFlyteClient
    :return:
    """

    command = _COMMAND.get()
    cli_logger.debug("Starting external process to generate id token. Command {}".format(command))
    try:
        output = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        cli_logger.error("Failed to generate token from command {}".format(command))
        raise _user_exceptions.FlyteAuthenticationException("Problems refreshing token with command: " + str(e))
    flyte_client.set_access_token(output.stdout.strip())


def _refresh_credentials_noop(flyte_client):
    pass


def _get_refresh_handler(auth_mode):
    if auth_mode == "standard":
        return _refresh_credentials_standard
    elif auth_mode == "basic" or auth_mode == "client_credentials":
        return _refresh_credentials_basic
    elif auth_mode == "external_process":
        return _refresh_credentials_from_command
    else:
        raise ValueError(
            "Invalid auth mode [{}] specified. Please update the creds config to use a valid value".format(auth_mode)
        )


def _handle_rpc_error(retry=False):
    def decorator(fn):
        def handler(*args, **kwargs):
            """
            Wraps rpc errors as Flyte exceptions and handles authentication the client.
            :param args:
            :param kwargs:
            :return:
            """
            max_retries = 3
            max_wait_time = 1000

            for i in range(max_retries):
                try:
                    return fn(*args, **kwargs)
                except _RpcError as e:
                    if e.code() == _GrpcStatusCode.UNAUTHENTICATED:
                        # Always retry auth errors.
                        if i == (max_retries - 1):
                            # Exit the loop and wrap the authentication error.
                            raise _user_exceptions.FlyteAuthenticationException(str(e))
                        cli_logger.error(f"Unauthenticated RPC error {e}, refreshing credentials and retrying\n")
                        refresh_handler_fn = _get_refresh_handler(creds_config.AUTH_MODE.get())
                        refresh_handler_fn(args[0])
                    # There are two cases that we should throw error immediately
                    # 1. Entity already exists when we register entity
                    # 2. Entity not found when we fetch entity
                    elif e.code() == _GrpcStatusCode.ALREADY_EXISTS:
                        raise _user_exceptions.FlyteEntityAlreadyExistsException(e)
                    elif e.code() == _GrpcStatusCode.NOT_FOUND:
                        raise _user_exceptions.FlyteEntityNotExistException(e)
                    else:
                        # No more retries if retry=False or max_retries reached.
                        if (retry is False) or i == (max_retries - 1):
                            raise
                        else:
                            # Retry: Start with 200ms wait-time and exponentially back-off up to 1 second.
                            wait_time = min(200 * (2 ** i), max_wait_time)
                            cli_logger.error(f"Non-auth RPC error {e}, sleeping {wait_time}ms and retrying")
                            time.sleep(wait_time / 1000)

        return handler

    return decorator


def _handle_invalid_create_request(fn):
    def handler(self, create_request):
        try:
            fn(self, create_request)
        except _RpcError as e:
            if e.code() == _GrpcStatusCode.INVALID_ARGUMENT:
                cli_logger.error("Error creating Flyte entity because of invalid arguments. Create request: ")
                cli_logger.error(_MessageToJson(create_request))

            # In any case, re-raise since we're not truly handling the error here
            raise e

    return handler


class RawSynchronousFlyteClient(object):
    """
    This is a thin synchronous wrapper around the auto-generated GRPC stubs for communicating with the admin service.

    This client should be usable regardless of environment in which this is used. In other words, configurations should
    be explicit as opposed to inferred from the environment or a configuration file.
    """

    def __init__(self, url, insecure=False, credentials=None, options=None, root_cert_file=None):
        """
        Initializes a gRPC channel to the given Flyte Admin service.

        :param Text url: The URL (including port if necessary) to connect to the appropriate Flyte Admin Service.
        :param bool insecure: [Optional] Whether to use an insecure connection, default False
        :param Text credentials: [Optional] If provided, a secure channel will be opened with the Flyte Admin Service.
        :param dict[Text, Text] options: [Optional] A dict of key-value string pairs for configuring the gRPC core
            runtime.
        :param root_cert_file: Path to a local certificate file if you want.
        """
        self._channel = None
        self._url = url

        if insecure:
            self._channel = _insecure_channel(url, options=list((options or {}).items()))
        else:
            if root_cert_file:
                with open(root_cert_file, "rb") as fh:
                    cert_bytes = fh.read()
                channel_creds = _ssl_channel_credentials(root_certificates=cert_bytes)
            else:
                channel_creds = _ssl_channel_credentials()

            self._channel = _secure_channel(
                url,
                credentials or channel_creds,
                options=list((options or {}).items()),
            )
        self._stub = _admin_service.AdminServiceStub(self._channel)
        self._auth_stub = auth_service.AuthMetadataServiceStub(self._channel)
        try:
            resp = self._auth_stub.GetPublicClientConfig(auth_pb2.PublicClientAuthConfigRequest())
            self._public_client_config = resp
        except _RpcError:
            cli_logger.debug("No public client auth config found, skipping.")
            self._public_client_config = None
        try:
            resp = self._auth_stub.GetOAuth2Metadata(auth_pb2.OAuth2MetadataRequest())
            self._oauth2_metadata = resp
        except _RpcError:
            cli_logger.debug("No OAuth2 Metadata found, skipping.")
            self._oauth2_metadata = None

        # metadata will hold the value of the token to send to the various endpoints.
        self._metadata = None

    @property
    def public_client_config(self) -> Optional[auth_pb2.PublicClientAuthConfigResponse]:
        return self._public_client_config

    @property
    def oauth2_metadata(self) -> Optional[auth_pb2.OAuth2MetadataResponse]:
        return self._oauth2_metadata

    @property
    def url(self) -> str:
        return self._url

    def set_access_token(self, access_token: str, authorization_header_key: Optional[str] = "authorization"):
        # Always set the header to lower-case regardless of what the config is. The grpc libraries that Admin uses
        # to parse the metadata don't change the metadata, but they do automatically lower the key you're looking for.
        cli_logger.debug(f"Adding authorization header. Header name: {authorization_header_key}.")
        self._metadata = [
            (
                authorization_header_key,
                f"Bearer {access_token}",
            )
        ]

    def check_access_token(self, access_token: str) -> bool:
        """
        This checks to see if the given access token is the same as the one already stored in the client. The reason
        this is useful is so that we can prevent unnecessary refreshing of tokens. Only if

        :param access_token: The access token to check
        :return:
        """
        if self._metadata is None:
            return False
        return access_token == self._metadata[0][1].replace("Bearer ", "")

    ####################################################################################################################
    #
    #  Task Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error()
    @_handle_invalid_create_request
    def create_task(self, task_create_request):
        """
        This will create a task definition in the Admin database. Once successful, the task object can be
        retrieved via the client or viewed via the UI or command-line interfaces.

        .. note ::

            Overwrites are not supported so any request for a given project, domain, name, and version that exists in
            the database must match the existing definition exactly. This also means that as long as the request
            remains identical, calling this method multiple times will result in success.

        :param: flyteidl.admin.task_pb2.TaskCreateRequest task_create_request: The request protobuf object.
        :rtype: flyteidl.admin.task_pb2.TaskCreateResponse
        :raises flytekit.common.exceptions.user.FlyteEntityAlreadyExistsException: If an identical version of the task
            is found, this exception is raised.  The client might choose to ignore this exception because the identical
            task is already registered.
        :raises grpc.RpcError:
        """
        return self._stub.CreateTask(task_create_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_task_ids_paginated(self, identifier_list_request):
        """
        This returns a page of identifiers for the tasks for a given project and domain. Filters can also be
        specified.

        .. note ::

            The name field in the TaskListRequest is ignored.

        .. note ::

            This is a paginated API.  Use the token field in the request to specify a page offset token.
            The user of the API is responsible for providing this token.

        .. note ::

            If entries are added to the database between requests for different pages, it is possible to receive
            entries on the second page that also appeared on the first.

        :param: flyteidl.admin.common_pb2.NamedEntityIdentifierListRequest identifier_list_request:
        :rtype: flyteidl.admin.common_pb2.NamedEntityIdentifierList
        :raises: TODO
        """
        return self._stub.ListTaskIds(identifier_list_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_tasks_paginated(self, resource_list_request):
        """
        This returns a page of task metadata for tasks in a given project and domain.  Optionally,
        specifying a name will limit the results to only tasks with that name in the given project and domain.

        .. note ::

            This is a paginated API.  Use the token field in the request to specify a page offset token.
            The user of the API is responsible for providing this token.

        .. note ::

            If entries are added to the database between requests for different pages, it is possible to receive
            entries on the second page that also appeared on the first.

        :param: flyteidl.admin.common_pb2.ResourceListRequest resource_list_request:
        :rtype: flyteidl.admin.task_pb2.TaskList
        :raises: TODO
        """
        return self._stub.ListTasks(resource_list_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_task(self, get_object_request):
        """
        This returns a single task for a given identifier.

        :param: flyteidl.admin.common_pb2.ObjectGetRequest get_object_request:
        :rtype: flyteidl.admin.task_pb2.Task
        :raises: TODO
        """
        return self._stub.GetTask(get_object_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Workflow Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error()
    @_handle_invalid_create_request
    def create_workflow(self, workflow_create_request):
        """
        This will create a workflow definition in the Admin database.  Once successful, the workflow object can be
        retrieved via the client or viewed via the UI or command-line interfaces.

        .. note ::

            Overwrites are not supported so any request for a given project, domain, name, and version that exists in
            the database must match the existing definition exactly.  This also means that as long as the request
            remains identical, calling this method multiple times will result in success.

        :param: flyteidl.admin.workflow_pb2.WorkflowCreateRequest workflow_create_request:
        :rtype: flyteidl.admin.workflow_pb2.WorkflowCreateResponse
        :raises flytekit.common.exceptions.user.FlyteEntityAlreadyExistsException: If an identical version of the
            workflow is found, this exception is raised.  The client might choose to ignore this exception because the
            identical workflow is already registered.
        :raises grpc.RpcError:
        """
        return self._stub.CreateWorkflow(workflow_create_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_workflow_ids_paginated(self, identifier_list_request):
        """
        This returns a page of identifiers for the workflows for a given project and domain. Filters can also be
        specified.

        .. note ::

            The name field in the WorkflowListRequest is ignored.

        .. note ::

            This is a paginated API.  Use the token field in the request to specify a page offset token.
            The user of the API is responsible for providing this token.

        .. note ::

            If entries are added to the database between requests for different pages, it is possible to receive
            entries on the second page that also appeared on the first.

        :param: flyteidl.admin.common_pb2.NamedEntityIdentifierListRequest identifier_list_request:
        :rtype: flyteidl.admin.common_pb2.NamedEntityIdentifierList
        :raises: TODO
        """
        return self._stub.ListWorkflowIds(identifier_list_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_workflows_paginated(self, resource_list_request):
        """
        This returns a page of workflow meta-information for workflows in a given project and domain.  Optionally,
        specifying a name will limit the results to only workflows with that name in the given project and domain.

        .. note ::

            This is a paginated API.  Use the token field in the request to specify a page offset token.
            The user of the API is responsible for providing this token.

        .. note ::

            If entries are added to the database between requests for different pages, it is possible to receive
            entries on the second page that also appeared on the first.

        :param: flyteidl.admin.common_pb2.ResourceListRequest resource_list_request:
        :rtype: flyteidl.admin.workflow_pb2.WorkflowList
        :raises: TODO
        """
        return self._stub.ListWorkflows(resource_list_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_workflow(self, get_object_request):
        """
        This returns a single workflow for a given identifier.

        :param: flyteidl.admin.common_pb2.ObjectGetRequest get_object_request:
        :rtype: flyteidl.admin.workflow_pb2.Workflow
        :raises: TODO
        """
        return self._stub.GetWorkflow(get_object_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Launch Plan Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error()
    @_handle_invalid_create_request
    def create_launch_plan(self, launch_plan_create_request):
        """
        This will create a launch plan definition in the Admin database.  Once successful, the launch plan object can be
        retrieved via the client or viewed via the UI or command-line interfaces.

        .. note ::

            Overwrites are not supported so any request for a given project, domain, name, and version that exists in
            the database must match the existing definition exactly.  This also means that as long as the request
            remains identical, calling this method multiple times will result in success.

        :param: flyteidl.admin.launch_plan_pb2.LaunchPlanCreateRequest launch_plan_create_request:  The request
            protobuf object
        :rtype: flyteidl.admin.launch_plan_pb2.LaunchPlanCreateResponse
        :raises flytekit.common.exceptions.user.FlyteEntityAlreadyExistsException: If an identical version of the
            launch plan is found, this exception is raised.  The client might choose to ignore this exception because
            the identical launch plan is already registered.
        :raises grpc.RpcError:
        """
        return self._stub.CreateLaunchPlan(launch_plan_create_request, metadata=self._metadata)

    # TODO: List endpoints when they come in

    @_handle_rpc_error(retry=True)
    def get_launch_plan(self, object_get_request):
        """
        Retrieves a launch plan entity.

        :param flyteidl.admin.common_pb2.ObjectGetRequest object_get_request:
        :rtype: flyteidl.admin.launch_plan_pb2.LaunchPlan
        """
        return self._stub.GetLaunchPlan(object_get_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_active_launch_plan(self, active_launch_plan_request):
        """
        Retrieves a launch plan entity.

        :param flyteidl.admin.common_pb2.ActiveLaunchPlanRequest active_launch_plan_request:
        :rtype: flyteidl.admin.launch_plan_pb2.LaunchPlan
        """
        return self._stub.GetActiveLaunchPlan(active_launch_plan_request, metadata=self._metadata)

    @_handle_rpc_error()
    def update_launch_plan(self, update_request):
        """
        Allows updates to a launch plan at a given identifier.  Currently, a launch plan may only have it's state
        switched between ACTIVE and INACTIVE.

        :param flyteidl.admin.launch_plan_pb2.LaunchPlanUpdateRequest update_request:
        :rtype: flyteidl.admin.launch_plan_pb2.LaunchPlanUpdateResponse
        """
        return self._stub.UpdateLaunchPlan(update_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_launch_plan_ids_paginated(self, identifier_list_request):
        """
        Lists launch plan named identifiers for a given project and domain.

        :param: flyteidl.admin.common_pb2.NamedEntityIdentifierListRequest identifier_list_request:
        :rtype: flyteidl.admin.common_pb2.NamedEntityIdentifierList
        """
        return self._stub.ListLaunchPlanIds(identifier_list_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_launch_plans_paginated(self, resource_list_request):
        """
        Lists Launch Plans for a given Identifier (project, domain, name)

        :param: flyteidl.admin.common_pb2.ResourceListRequest resource_list_request:
        :rtype: flyteidl.admin.launch_plan_pb2.LaunchPlanList
        """
        return self._stub.ListLaunchPlans(resource_list_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_active_launch_plans_paginated(self, active_launch_plan_list_request):
        """
        Lists Active Launch Plans for a given (project, domain)

        :param: flyteidl.admin.common_pb2.ActiveLaunchPlanListRequest active_launch_plan_list_request:
        :rtype: flyteidl.admin.launch_plan_pb2.LaunchPlanList
        """
        return self._stub.ListActiveLaunchPlans(active_launch_plan_list_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Named Entity Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error()
    def update_named_entity(self, update_named_entity_request):
        """
        :param flyteidl.admin.common_pb2.NamedEntityUpdateRequest update_named_entity_request:
        :rtype: flyteidl.admin.common_pb2.NamedEntityUpdateResponse
        """
        return self._stub.UpdateNamedEntity(update_named_entity_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Workflow Execution Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error()
    def create_execution(self, create_execution_request):
        """
        This will create an execution for the given execution spec.
        :param flyteidl.admin.execution_pb2.ExecutionCreateRequest create_execution_request:
        :rtype: flyteidl.admin.execution_pb2.ExecutionCreateResponse
        """
        return self._stub.CreateExecution(create_execution_request, metadata=self._metadata)

    @_handle_rpc_error()
    def recover_execution(self, recover_execution_request):
        """
        This will recreate an execution with the same spec as the one belonging to the given execution identifier.
        :param flyteidl.admin.execution_pb2.ExecutionRecoverRequest recover_execution_request:
        :rtype: flyteidl.admin.execution_pb2.ExecutionRecoverResponse
        """
        return self._stub.RecoverExecution(recover_execution_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_execution(self, get_object_request):
        """
        Returns an execution of a workflow entity.

        :param flyteidl.admin.execution_pb2.WorkflowExecutionGetRequest get_object_request:
        :rtype: flyteidl.admin.execution_pb2.Execution
        """
        return self._stub.GetExecution(get_object_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_execution_data(self, get_execution_data_request):
        """
        Returns signed URLs to LiteralMap blobs for an execution's inputs and outputs (when available).

        :param flyteidl.admin.execution_pb2.WorkflowExecutionGetRequest get_execution_data_request:
        :rtype: flyteidl.admin.execution_pb2.WorkflowExecutionGetDataResponse
        """
        return self._stub.GetExecutionData(get_execution_data_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_executions_paginated(self, resource_list_request):
        """
        Lists the executions for a given identifier.

        :param flyteidl.admin.common_pb2.ResourceListRequest resource_list_request:
        :rtype: flyteidl.admin.execution_pb2.ExecutionList
        """
        return self._stub.ListExecutions(resource_list_request, metadata=self._metadata)

    @_handle_rpc_error()
    def terminate_execution(self, terminate_execution_request):
        """
        :param flyteidl.admin.execution_pb2.TerminateExecutionRequest terminate_execution_request:
        :rtype: flyteidl.admin.execution_pb2.TerminateExecutionResponse
        """
        return self._stub.TerminateExecution(terminate_execution_request, metadata=self._metadata)

    @_handle_rpc_error()
    def relaunch_execution(self, relaunch_execution_request):
        """
        :param flyteidl.admin.execution_pb2.ExecutionRelaunchRequest relaunch_execution_request:
        :rtype: flyteidl.admin.execution_pb2.ExecutionCreateResponse
        """
        return self._stub.RelaunchExecution(relaunch_execution_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Node Execution Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error(retry=True)
    def get_node_execution(self, node_execution_request):
        """
        :param flyteidl.admin.node_execution_pb2.NodeExecutionGetRequest node_execution_request:
        :rtype: flyteidl.admin.node_execution_pb2.NodeExecution
        """
        return self._stub.GetNodeExecution(node_execution_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_node_execution_data(self, get_node_execution_data_request):
        """
        Returns signed URLs to LiteralMap blobs for a node execution's inputs and outputs (when available).

        :param flyteidl.admin.node_execution_pb2.NodeExecutionGetDataRequest get_node_execution_data_request:
        :rtype: flyteidl.admin.node_execution_pb2.NodeExecutionGetDataResponse
        """
        return self._stub.GetNodeExecutionData(get_node_execution_data_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_node_executions_paginated(self, node_execution_list_request):
        """
        :param flyteidl.admin.node_execution_pb2.NodeExecutionListRequest node_execution_list_request:
        :rtype: flyteidl.admin.node_execution_pb2.NodeExecutionList
        """
        return self._stub.ListNodeExecutions(node_execution_list_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_node_executions_for_task_paginated(self, node_execution_for_task_list_request):
        """
        :param flyteidl.admin.node_execution_pb2.NodeExecutionListRequest node_execution_for_task_list_request:
        :rtype: flyteidl.admin.node_execution_pb2.NodeExecutionList
        """
        return self._stub.ListNodeExecutionsForTask(node_execution_for_task_list_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Task Execution Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error(retry=True)
    def get_task_execution(self, task_execution_request):
        """
        :param flyteidl.admin.task_execution_pb2.TaskExecutionGetRequest task_execution_request:
        :rtype: flyteidl.admin.task_execution_pb2.TaskExecution
        """
        return self._stub.GetTaskExecution(task_execution_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_task_execution_data(self, get_task_execution_data_request):
        """
        Returns signed URLs to LiteralMap blobs for a task execution's inputs and outputs (when available).

        :param flyteidl.admin.task_execution_pb2.TaskExecutionGetDataRequest get_task_execution_data_request:
        :rtype: flyteidl.admin.task_execution_pb2.TaskExecutionGetDataResponse
        """
        return self._stub.GetTaskExecutionData(get_task_execution_data_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_task_executions_paginated(self, task_execution_list_request):
        """
        :param flyteidl.admin.task_execution_pb2.TaskExecutionListRequest task_execution_list_request:
        :rtype: flyteidl.admin.task_execution_pb2.TaskExecutionList
        """
        return self._stub.ListTaskExecutions(task_execution_list_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Project Endpoints
    #
    ####################################################################################################################

    @_handle_rpc_error(retry=True)
    def list_projects(self, project_list_request):
        """
        This will return a list of the projects registered with the Flyte Admin Service
        :param flyteidl.admin.project_pb2.ProjectListRequest project_list_request:
        :rtype: flyteidl.admin.project_pb2.Projects
        """
        return self._stub.ListProjects(project_list_request, metadata=self._metadata)

    @_handle_rpc_error()
    def register_project(self, project_register_request):
        """
        Registers a project along with a set of domains.
        :param flyteidl.admin.project_pb2.ProjectRegisterRequest project_register_request:
        :rtype: flyteidl.admin.project_pb2.ProjectRegisterResponse
        """
        return self._stub.RegisterProject(project_register_request, metadata=self._metadata)

    @_handle_rpc_error()
    def update_project(self, project):
        """
        Update an existing project specified by id.
        :param flyteidl.admin.project_pb2.Project project:
        :rtype: flyteidl.admin.project_pb2.ProjectUpdateResponse
        """
        return self._stub.UpdateProject(project, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Matching Attributes Endpoints
    #
    ####################################################################################################################
    @_handle_rpc_error()
    def update_project_domain_attributes(self, project_domain_attributes_update_request):
        """
        This updates the attributes for a project and domain registered with the Flyte Admin Service
        :param flyteidl.admin.ProjectDomainAttributesUpdateRequest project_domain_attributes_update_request:
        :rtype: flyteidl.admin.ProjectDomainAttributesUpdateResponse
        """
        return self._stub.UpdateProjectDomainAttributes(
            project_domain_attributes_update_request, metadata=self._metadata
        )

    @_handle_rpc_error()
    def update_workflow_attributes(self, workflow_attributes_update_request):
        """
        This updates the attributes for a project, domain, and workflow registered with the Flyte Admin Service
        :param flyteidl.admin.UpdateWorkflowAttributesRequest workflow_attributes_update_request:
        :rtype: flyteidl.admin.WorkflowAttributesUpdateResponse
        """
        return self._stub.UpdateWorkflowAttributes(workflow_attributes_update_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_project_domain_attributes(self, project_domain_attributes_get_request):
        """
        This fetches the attributes for a project and domain registered with the Flyte Admin Service
        :param flyteidl.admin.ProjectDomainAttributesGetRequest project_domain_attributes_get_request:
        :rtype: flyteidl.admin.ProjectDomainAttributesGetResponse
        """
        return self._stub.GetProjectDomainAttributes(project_domain_attributes_get_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def get_workflow_attributes(self, workflow_attributes_get_request):
        """
        This fetches the attributes for a project, domain, and workflow registered with the Flyte Admin Service
        :param flyteidl.admin.GetWorkflowAttributesAttributesRequest workflow_attributes_get_request:
        :rtype: flyteidl.admin.WorkflowAttributesGetResponse
        """
        return self._stub.GetWorkflowAttributes(workflow_attributes_get_request, metadata=self._metadata)

    @_handle_rpc_error(retry=True)
    def list_matchable_attributes(self, matchable_attributes_list_request):
        """
        This fetches the attributes for a specific resource type registered with the Flyte Admin Service
        :param flyteidl.admin.ListMatchableAttributesRequest matchable_attributes_list_request:
        :rtype: flyteidl.admin.ListMatchableAttributesResponse
        """
        return self._stub.ListMatchableAttributes(matchable_attributes_list_request, metadata=self._metadata)

    ####################################################################################################################
    #
    #  Event Endpoints
    #
    ####################################################################################################################

    # TODO: (P2) Implement the event endpoints in case there becomes a use-case for third-parties to submit events
    # through the client in Python.


def get_token(token_endpoint, authorization_header, scope):
    """
    :param Text token_endpoint:
    :param Text authorization_header: This is the value for the "Authorization" key. (eg 'Bearer abc123')
    :param Text scope:
    :rtype: (Text,Int) The first element is the access token retrieved from the IDP, the second is the expiration
            in seconds
    """
    headers = {
        "Authorization": authorization_header,
        "Cache-Control": "no-cache",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = {
        "grant_type": "client_credentials",
    }
    if scope is not None:
        body["scope"] = scope
    response = _requests.post(token_endpoint, data=body, headers=headers)
    if response.status_code != 200:
        _logging.error("Non-200 ({}) received from IDP: {}".format(response.status_code, response.text))
        raise FlyteAuthenticationException("Non-200 received from IDP")

    response = response.json()
    return response["access_token"], response["expires_in"]


def get_secret():
    """
    This function will either read in the password from the file path given by the CLIENT_CREDENTIALS_SECRET_LOCATION
    config object, or from the environment variable using the CLIENT_CREDENTIALS_SECRET config object.
    :rtype: Text
    """
    secret = _CREDENTIALS_SECRET.get()
    if secret:
        return secret
    raise FlyteAuthenticationException("No secret could be found")


def get_basic_authorization_header(client_id, client_secret):
    """
    This function transforms the client id and the client secret into a header that conforms with http basic auth.
    It joins the id and the secret with a : then base64 encodes it, then adds the appropriate text.
    :param Text client_id:
    :param Text client_secret:
    :rtype: Text
    """
    concated = "{}:{}".format(client_id, client_secret)
    return "Basic {}".format(_base64.b64encode(concated.encode(_utf_8)).decode(_utf_8))


_utf_8 = "utf-8"
