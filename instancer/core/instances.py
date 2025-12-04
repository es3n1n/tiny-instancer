import asyncio
import http
import uuid
from enum import StrEnum
from functools import cache

from aiodocker import Docker, DockerError
from aiodocker.containers import DockerContainer
from fastapi import HTTPException

from instancer.core.cache import instance_lock
from instancer.core.config import config
from instancer.protocol import types as protocol
from instancer.util.logger import logger
from instancer.util.time import timestamp_milliseconds


NOT_ACQUIRED_ERROR = HTTPException(status_code=400, detail='Another instance operation is in progress.')


class ContainerLabels(StrEnum):
    MANAGED_BY = 'io.es3n1n.managed_by'
    CHALLENGE = 'io.es3n1n.instancer.challenge'
    TEAM_ID = 'io.es3n1n.instancer.team_id'
    TARGET_HOSTNAME = 'io.es3n1n.instancer.hostname'
    INSTANCE_ID = 'io.es3n1n.instancer.instance_id'
    STARTED_AT = 'io.es3n1n.instancer.started_at'
    EXPIRES_AT = 'io.es3n1n.instancer.expires_at'
    EXPOSED_KINDS = 'io.es3n1n.instancer.exposed_kinds'


@cache
def get_docker() -> Docker:
    # FIXME(es3n1n): This is a workaround for prunner process not having a loop at import time.
    return Docker()


def _get_search_filters(challenge_name: str, team_id: str) -> dict[str, dict | list | str]:
    return {
        'label': [
            f'{ContainerLabels.MANAGED_BY}={config.DOCKER_MANAGER_NAME}',
            f'{ContainerLabels.CHALLENGE}={challenge_name}',
            f'{ContainerLabels.TEAM_ID}={team_id}',
        ]
    }


def _get_endpoints_for(expose_kinds: str, host: str) -> list[protocol.RCTFInstanceDetails.Endpoint]:
    result = []

    for kind_s in expose_kinds.split(','):
        kind = protocol.ExposeKind(kind_s)
        if kind == protocol.ExposeKind.TCP:
            # We expose only TCP_SSL, due to SNI host matching for routing
            kind = protocol.ExposeKind.TCP_SSL

        port = 1337
        match kind:
            case protocol.ExposeKind.HTTP:
                port = 80
            case protocol.ExposeKind.HTTPS:
                port = 443

        result.append(
            protocol.RCTFInstanceDetails.Endpoint(
                kind=kind,
                host=host,
                port=port,
            )
        )

    return result


async def get_containers(
    challenge_name: str,
    team_id: str,
    *,
    limit: int | None = None,
    running_only: bool = False,
) -> list[DockerContainer]:
    kwargs: dict[str, int] = {}
    if limit is not None:
        kwargs['limit'] = limit
    try:
        return await get_docker().containers.list(
            all=not running_only, filters=_get_search_filters(challenge_name, team_id), **kwargs
        )
    except DockerError as err:
        logger.opt(exception=err).error(f'Error getting containers: {challenge_name=} {team_id=}')
        return []


async def is_running(challenge_name: str, team_id: str) -> bool:
    return bool(await get_containers(challenge_name, team_id, running_only=True, limit=1))


async def _ensure_network(name: str, *, internal: bool, expires_at: int) -> None:
    try:
        network = await get_docker().networks.get(name)
    except DockerError:
        try:
            network = await get_docker().networks.create(
                {
                    'Name': name,
                    'Driver': 'bridge',
                    'Internal': internal,
                    'Labels': {
                        ContainerLabels.MANAGED_BY: config.DOCKER_MANAGER_NAME,
                        ContainerLabels.EXPIRES_AT: str(expires_at),
                    },
                }
            )
        except DockerError as err:
            if err.status == http.HTTPStatus.BAD_REQUEST.value and 'fully subnetted' in (err.message or ''):
                raise HTTPException(
                    status_code=500,
                    detail='Daemon has run out of available subnets for creating networks. Contact admins.',
                ) from err
            raise

    # Connect traefik if internal
    if internal:
        try:
            await network.connect(
                {
                    'Container': config.TRAEFIK_CONTAINER_NAME,
                }
            )
        except DockerError as err:
            if err.status != http.HTTPStatus.CONFLICT.value:
                raise


def _add_expose_labels(
    host: str,
    labels: dict[str, str | list[str]],
    form: protocol.RCTFCreateInstanceForm,
    pod: protocol.RCTFCreateInstanceForm.Pod,
    instance_id: str,
) -> None:
    # All endpoints share the same hostname, so no need to check for container name here
    has_http_expose = any(e.kind == protocol.ExposeKind.HTTP for e in form.expose)

    for i, expose in enumerate(form.expose):
        if expose.pod_name != pod.name:
            continue

        router_name = f'{config.PREFIX}-{form.challenge_integration_id}-{form.team_id}-{instance_id}-{pod.name}-{i}'

        match expose.kind:
            case protocol.ExposeKind.TCP | protocol.ExposeKind.TCP_SSL:
                labels[f'traefik.tcp.routers.{router_name}.rule'] = f'HostSNI(`{host}`)'
                labels[f'traefik.tcp.routers.{router_name}.entrypoints'] = config.TRAEFIK_TCP_ENTRYPOINT
                labels[f'traefik.tcp.routers.{router_name}.service'] = router_name
                labels[f'traefik.tcp.routers.{router_name}.tls.passthrough'] = 'true'
                labels[f'traefik.tcp.services.{router_name}.loadbalancer.server.port'] = str(expose.pod_port)

            case protocol.ExposeKind.HTTP:
                labels[f'traefik.http.routers.{router_name}.rule'] = f'Host(`{host}`)'
                labels[f'traefik.http.routers.{router_name}.entrypoints'] = config.TRAEFIK_HTTP_ENTRYPOINT
                labels[f'traefik.http.routers.{router_name}.service'] = router_name
                labels[f'traefik.http.services.{router_name}.loadbalancer.server.port'] = str(expose.pod_port)

            case protocol.ExposeKind.HTTPS:
                labels[f'traefik.http.routers.{router_name}.rule'] = f'Host(`{host}`)'
                labels[f'traefik.http.routers.{router_name}.entrypoints'] = config.TRAEFIK_HTTPS_ENTRYPOINT
                labels[f'traefik.http.routers.{router_name}.tls'] = 'true'
                labels[f'traefik.http.routers.{router_name}.service'] = router_name
                labels[f'traefik.http.services.{router_name}.loadbalancer.server.port'] = str(expose.pod_port)

                if not has_http_expose:
                    redirect_router_name = f'{router_name}-redirect'
                    labels[f'traefik.http.routers.{redirect_router_name}.rule'] = f'Host(`{host}`)'
                    labels[f'traefik.http.routers.{redirect_router_name}.entrypoints'] = config.TRAEFIK_HTTP_ENTRYPOINT
                    labels[f'traefik.http.routers.{redirect_router_name}.middlewares'] = (
                        config.TRAEFIK_PERMANENT_REDIRECT_MIDDLEWARE_NAME
                    )


async def cleanup_containers(containers: list[tuple[str, DockerContainer]]) -> None:
    if not containers:
        return

    delete_coroutines: list = []
    names: list[str] = []
    for name, container in containers:
        delete_coroutines.append(container.delete(force=True))
        names.append(name)

    await asyncio.gather(*delete_coroutines)


async def cleanup_networks(names: list[str]) -> None:
    if not names:
        return

    delete_coroutines: list = []
    existing_names: list[str] = []
    for name in names:
        try:
            network = await get_docker().networks.get(name)
        except DockerError as err:
            if err.status != http.HTTPStatus.NOT_FOUND.value:
                logger.opt(exception=err).warning(f'Failed to fetch network during rollback cleanup: {name}')
            continue

        details = await network.show()
        for conn in (details['Containers'] or {}).values():
            logger.info(f'Disconnecting container {conn["Name"]} from network {name} during rollback cleanup')
            await network.disconnect(
                {
                    'Container': conn['Name'],
                    'Force': True,
                },
            )

        delete_coroutines.append(network.delete())
        existing_names.append(name)

    if not delete_coroutines:
        return

    await asyncio.gather(*delete_coroutines)


async def start_instance(form: protocol.RCTFCreateInstanceForm) -> protocol.RCTFInstanceDetails:
    async with instance_lock(form.challenge_integration_id, form.team_id) as acquired:
        if not acquired:
            raise NOT_ACQUIRED_ERROR

        if await is_running(form.challenge_integration_id, form.team_id):
            raise HTTPException(status_code=400, detail='Instance is already running')

        exposed_kinds = ''.join(k.kind for k in form.expose)
        started_at = timestamp_milliseconds()
        expires_at = started_at + form.timeout_milliseconds

        instance_id = uuid.uuid4().hex[:12]
        host = f'{form.challenge_integration_id}-{instance_id}.{config.INSTANCES_HOST}'

        svc_net = f'{config.PREFIX}-svc-{form.challenge_integration_id}-{form.team_id}-{instance_id}'
        eg_net = f'{config.PREFIX}-eg-{form.challenge_integration_id}-{form.team_id}-{instance_id}'

        created_containers: list[tuple[str, DockerContainer]] = []
        networks_created: list[str] = []

        try:
            await _ensure_network(svc_net, internal=True, expires_at=expires_at)
            networks_created.append(svc_net)

            if any(c.egress for c in form.pods):
                await _ensure_network(eg_net, internal=False, expires_at=expires_at)
                networks_created.append(eg_net)

            for container in form.pods:
                try:
                    await get_docker().images.get(container.image)
                except DockerError:
                    await get_docker().images.pull(container.image)

                labels: dict[str, str | list[str]] = {
                    ContainerLabels.MANAGED_BY: config.DOCKER_MANAGER_NAME,
                    ContainerLabels.CHALLENGE: form.challenge_integration_id,
                    ContainerLabels.TEAM_ID: form.team_id,
                    ContainerLabels.TARGET_HOSTNAME: host,
                    ContainerLabels.STARTED_AT: str(started_at),
                    ContainerLabels.EXPIRES_AT: str(expires_at),
                    ContainerLabels.INSTANCE_ID: instance_id,
                    ContainerLabels.EXPOSED_KINDS: exposed_kinds,
                }

                if form.expose:
                    labels['traefik.enable'] = 'true'
                    labels['traefik.docker.network'] = svc_net
                _add_expose_labels(host, labels, form, container, instance_id)

                # Setup networking
                endpoints_config: dict[str, dict] = {
                    svc_net: {},
                }
                if container.egress:
                    endpoints_config[eg_net] = {}

                container_name = f'{config.PREFIX}-{form.challenge_integration_id}-{form.team_id}-{container.name}'
                logger.info(f'Spinning up container {container_name=} {form.challenge_integration_id=} {form.team_id=}')
                created_container = await get_docker().containers.create(
                    config={
                        'Hostname': container.name,
                        'Image': container.image,
                        'Env': [f'{k}={v}' for k, v in container.env.items()],
                        'Labels': labels,
                        'HostConfig': {
                            'RestartPolicy': {
                                'Name': 'unless-stopped',
                            },
                            'ReadOnlyRootfs': container.security.read_only_fs,
                            'Tmpfs': {'/tmp': 'noexec,nosuid,nodev'} if container.security.read_only_fs else {},  # noqa: S108
                            'SecurityOpt': container.security.docker_security_opt,
                            'Memory': container.limits.memory_bytes,
                            'MemorySwap': container.limits.memory_bytes,
                            'NanoCpus': container.limits.cpus_nano,
                            'PidsLimit': container.limits.pids_limit,
                            'CapAdd': container.security.cap_add,
                            'CapDrop': container.security.cap_drop,
                            'LogConfig': {
                                'Type': 'json-file',
                            },
                            'Ulimits': [
                                {'Name': ulimit.name, 'Soft': ulimit.soft, 'Hard': ulimit.hard}
                                for ulimit in container.limits.ulimits
                            ],
                        },
                        'NetworkingConfig': {
                            'EndpointsConfig': endpoints_config,
                        },
                    },
                    name=container_name,
                )
                created_containers.append((container_name, created_container))

            start_tasks = [container.start() for _, container in created_containers]
            await asyncio.gather(*start_tasks)
        except Exception as err:
            await cleanup_containers(created_containers)
            await cleanup_networks(networks_created)

            if isinstance(err, HTTPException):
                raise

            logger.opt(exception=err).error(
                f'Failed to start instance: {form.challenge_integration_id=} {form.team_id=}'
            )
            raise HTTPException(status_code=500, detail='Failed to start instance') from err

        return protocol.RCTFInstanceDetails(
            status=protocol.InstanceStatus.STARTING,
            time_left_milliseconds=expires_at - timestamp_milliseconds(),
            endpoints=_get_endpoints_for(exposed_kinds, host),
        )


async def stop_instance(challenge_name: str, team_id: str) -> protocol.RCTFInstanceDetails:
    async with instance_lock(challenge_name, team_id) as acquired:
        if not acquired:
            raise NOT_ACQUIRED_ERROR

        instance_containers = await get_containers(challenge_name, team_id)
        if not instance_containers:
            raise HTTPException(status_code=404, detail='Instance not found')

        networks_to_remove: set[str] = set()
        stop_tasks = []

        for container in instance_containers:
            details = await container.show()
            for net_name in details['NetworkSettings']['Networks']:
                # Remove only our stuff
                if not net_name.startswith(f'{config.PREFIX}-'):
                    continue

                networks_to_remove.add(net_name)

            logger.info(f'Stopping container {container.id=} {challenge_name=} {team_id=}')
            stop_tasks.append(container.stop(t=config.DOCKER_STOP_TIMEOUT_SECONDS))

        await asyncio.gather(*stop_tasks, return_exceptions=True)
        await asyncio.gather(*[c.delete(force=True) for c in instance_containers], return_exceptions=True)
        logger.info(f'Removed {len(instance_containers)} containers.')

        net_remove_tasks = []
        net_disconnect_tasks = []
        for net_name in networks_to_remove:
            network = await get_docker().networks.get(net_name)

            # Disconnect everyone.
            # Doing show for the second time to reflect changes after container deletions.
            details = await network.show()
            for conn in (details['Containers'] or {}).values():
                logger.info(f'Disconnecting container {conn["Name"]} from network {net_name}')
                net_disconnect_tasks.append(
                    network.disconnect(
                        {
                            'Container': conn['Name'],
                            'Force': True,
                        },
                    )
                )

            logger.info(f'Removing network {net_name}')
            net_remove_tasks.append(network.delete())

        await asyncio.gather(*net_disconnect_tasks, return_exceptions=True)
        await asyncio.gather(*net_remove_tasks, return_exceptions=True)
        logger.info(f'Removed {len(networks_to_remove)} networks.')
        return protocol.RCTFInstanceDetails(
            status=protocol.InstanceStatus.STOPPED,
            endpoints=None,
            time_left_milliseconds=None,
        )


async def get_instance(challenge_name: str, team_id: str) -> protocol.RCTFInstanceDetails:
    containers = await get_containers(challenge_name, team_id, limit=1)

    status = protocol.InstanceStatus.STOPPED
    exposed_kinds: str | None = None
    expires_at: int | None = None
    host: str | None = None
    if containers:
        details = await containers[0].show()
        labels = details['Config']['Labels']
        state = details['State']['Status']

        expires_at = int(labels[ContainerLabels.EXPIRES_AT])
        host = labels[ContainerLabels.TARGET_HOSTNAME]
        status = protocol.InstanceStatus.RUNNING if state == 'running' else protocol.InstanceStatus.STARTING
        exposed_kinds = labels[ContainerLabels.EXPOSED_KINDS]

    return protocol.RCTFInstanceDetails(
        status=status,
        endpoints=_get_endpoints_for(exposed_kinds, host) if exposed_kinds and host else None,
        time_left_milliseconds=max(0, expires_at - timestamp_milliseconds()) if expires_at else None,
    )
