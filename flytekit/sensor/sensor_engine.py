import importlib
import typing
from typing import Optional

import cloudpickle
import grpc
import jsonpickle
from flyteidl.admin.agent_pb2 import (
    PENDING,
    RUNNING,
    SUCCEEDED,
    CreateTaskResponse,
    DeleteTaskResponse,
    GetTaskResponse,
    Resource,
)
from flytekit.models.core.execution import TaskLog
from flytekit import FlyteContextManager
from flytekit.core.type_engine import TypeEngine
from flytekit.extend.backend.base_agent import AgentBase, AgentRegistry
from flytekit.models.literals import LiteralMap
from flytekit.models.task import TaskTemplate
from flytekit.sensor.base_sensor import INPUTS, SENSOR_CONFIG_PKL, SENSOR_MODULE, SENSOR_NAME

T = typing.TypeVar("T")


class SensorEngine(AgentBase):
    def __init__(self):
        super().__init__(task_type="sensor", asynchronous=True)

    async def async_create(
        self,
        context: grpc.ServicerContext,
        output_prefix: str,
        task_template: TaskTemplate,
        inputs: Optional[LiteralMap] = None,
    ) -> CreateTaskResponse:
        python_interface_inputs = {
            name: TypeEngine.guess_python_type(lt.type) for name, lt in task_template.interface.inputs.items()
        }
        ctx = FlyteContextManager.current_context()
        if inputs:
            native_inputs = TypeEngine.literal_map_to_kwargs(ctx, inputs, python_interface_inputs)
            task_template.custom[INPUTS] = native_inputs
        return CreateTaskResponse(resource_meta=cloudpickle.dumps(task_template.custom))

    async def async_get(self, context: grpc.ServicerContext, resource_meta: bytes) -> GetTaskResponse:
        meta = cloudpickle.loads(resource_meta)

        sensor_module = importlib.import_module(name=meta[SENSOR_MODULE])
        sensor_def = getattr(sensor_module, meta[SENSOR_NAME])
        sensor_config = jsonpickle.decode(meta[SENSOR_CONFIG_PKL]) if meta.get(SENSOR_CONFIG_PKL) else None
    
        inputs = meta.get(INPUTS, {})
        # cur_state = RUNNING
        log_links = [TaskLog(uri="sensor.com", name="Sensor Console").to_flyte_idl()]
        cur_state = SUCCEEDED if await sensor_def("sensor", config=sensor_config).poke(**inputs) else PENDING
        cur_state = SUCCEEDED
        if cur_state == SUCCEEDED:
            log_links.append(TaskLog(uri="sensor.com", name="Sensor SUCCEEDED").to_flyte_idl())
        return GetTaskResponse(resource=Resource(state=cur_state, outputs=None, message="Sensor is running"), log_links=log_links)

    async def async_delete(self, context: grpc.ServicerContext, resource_meta: bytes) -> DeleteTaskResponse:
        return DeleteTaskResponse()


AgentRegistry.register(SensorEngine())
