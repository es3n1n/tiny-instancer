from enum import StrEnum
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr

from instancer.core.config import config


class BaseRCTFRequest(BaseModel):
    rctf_auth_token: SecretStr = Field(validation_alias='rctfAuthToken')

    def check_token(self) -> None:
        if self.rctf_auth_token.get_secret_value() != config.AUTH_TOKEN.get_secret_value():
            raise HTTPException(status_code=422, detail='Invalid token')


class ExposeKind(StrEnum):
    TCP = 'tcp'
    TCP_SSL = 'tcp-ssl'
    HTTP = 'http'
    HTTPS = 'https'


class InstanceStatus(StrEnum):
    STOPPED = 'stopped'
    RUNNING = 'running'
    STARTING = 'starting'
    ERRORED = 'errored'


class RCTFCreateInstanceForm(BaseRCTFRequest):
    class Pod(BaseModel):
        class Security(BaseModel):
            read_only_fs: bool = Field(validation_alias='readOnlyFs')
            docker_security_opt: list[str] = Field(validation_alias='dockerSecurityOpt')
            cap_add: list[str] = Field(validation_alias='capAdd')
            cap_drop: list[str] = Field(validation_alias='capDrop')

        class Limits(BaseModel):
            class Ulimit(BaseModel):
                name: str
                soft: int
                hard: int

            memory_bytes: int = Field(validation_alias='memoryBytes')
            cpus_nano: int = Field(validation_alias='cpusNano')
            pids_limit: int = Field(validation_alias='pidsLimit')
            ulimits: list[Ulimit]

        name: str
        image: str
        env: dict[str, str]
        egress: bool
        security: Security
        limits: Limits

    class Expose(BaseModel):
        kind: ExposeKind
        pod_name: str = Field(validation_alias='podName')
        pod_port: int = Field(validation_alias='podPort')

    kind: Literal['instancerCreateInstanceForm'] = 'instancerCreateInstanceForm'
    team_id: str = Field(validation_alias='teamId')
    challenge_integration_id: str = Field(validation_alias='challengeIntegrationId')
    pods: list[Pod]
    expose: list[Expose]
    timeout_milliseconds: int = Field(validation_alias='timeoutMilliseconds')


class RCTFGetInstanceForm(BaseRCTFRequest):
    kind: Literal['instancerGetInstanceForm'] = 'instancerGetInstanceForm'
    team_id: str = Field(validation_alias='teamId')
    challenge_integration_id: str = Field(validation_alias='challengeIntegrationId')


class RCTFStopInstanceForm(BaseRCTFRequest):
    kind: Literal['instancerStopInstanceForm'] = 'instancerStopInstanceForm'
    team_id: str = Field(validation_alias='teamId')
    challenge_integration_id: str = Field(validation_alias='challengeIntegrationId')


class RCTFInstanceDetails(BaseModel):
    class Endpoint(BaseModel):
        kind: ExposeKind
        host: str
        port: int

    kind: Literal['instancerInstanceDetails'] = 'instancerInstanceDetails'
    status: InstanceStatus
    time_left_milliseconds: int | None = Field(serialization_alias='timeLeftMilliseconds')
    endpoints: list[Endpoint] | None


class RCTFInstancerError(BaseModel):
    kind: Literal['instancerError'] = 'instancerError'
    message: str
