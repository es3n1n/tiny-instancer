from contextlib import suppress

from fastapi import HTTPException

from instancer.core.config import config
from instancer.core.instances import ContainerLabels, Docker, DockerError, cleanup_networks, get_docker, stop_instance
from instancer.util.logger import logger
from instancer.util.time import timestamp_milliseconds


try:
    from uvloop import run  # type: ignore[import-not-found]
except ImportError:
    from asyncio import run, sleep


async def _prune_instances(docker: Docker, now: int) -> None:
    # TODO(es3n1n): Is there a way how to query containers by label value comparison?
    containers = await docker.containers.list(
        all=True,
        filters={
            'label': [
                f'{ContainerLabels.MANAGED_BY}={config.DOCKER_MANAGER_NAME}',
            ],
        },
    )

    for container in containers:
        try:
            details = await container.show()
        except DockerError:
            # Got deleted already
            continue
        labels = details['Config']['Labels']

        expires_at = int(labels[ContainerLabels.EXPIRES_AT])
        if expires_at > now:
            continue

        challenge = labels[ContainerLabels.CHALLENGE]
        team_id = labels[ContainerLabels.TEAM_ID]
        logger.info(f'Prunner stopping expired container {container.id=} {challenge=} {team_id=} {expires_at=} {now=}')

        try:
            await stop_instance(challenge, team_id)
        except HTTPException as err:
            logger.opt(exception=err).warning(
                f'Prunner failed to stop expired container {container.id=} via stop_instance, will try again'
            )
        except DockerError as err:
            logger.opt(exception=err).warning(f'Prunner failed to remove expired container {container.id=}')


async def _prune_networks(docker: Docker, now: int) -> None:
    # TODO(es3n1n): Is there a way how to query containers by label value comparison?
    networks = await docker.networks.list(
        filters={
            'label': [
                f'{ContainerLabels.MANAGED_BY}={config.DOCKER_MANAGER_NAME}',
            ],
        }
    )

    names_to_prune: list[str] = []
    for network in networks:
        # NOTE(es3n1n): Going for a private method as I dont want to do the inspect request 2 times / network
        details = await docker._query_json(f'networks/{network["Id"]}', method='GET')  # noqa: SLF001
        labels = details['Labels']

        expires_at = int(labels[ContainerLabels.EXPIRES_AT])
        if expires_at > now:
            continue

        logger.info(f'Prunning expired network {network["Name"]=} {expires_at=} {now=}')
        names_to_prune.append(network['Name'])

    await cleanup_networks(names_to_prune)


async def instance_prunner() -> None:
    docker = get_docker()
    while True:
        now = timestamp_milliseconds()
        logger.info('Running instance prunner')
        try:
            await _prune_instances(docker, now)
            await _prune_networks(docker, now)
        except Exception as e:  # noqa: BLE001
            logger.opt(exception=e).error('Encountered an error while prunning')
        await sleep(config.PRUNNER_INTERVAL_SECONDS)


def prunner_process() -> None:
    with suppress(KeyboardInterrupt):
        run(instance_prunner())
