from fastapi import APIRouter

from instancer.core import instances
from instancer.protocol import types


router = APIRouter(
    prefix='/v1/instances',
    tags=['instances'],
)


@router.post('/')
async def get_instance(form: types.RCTFGetInstanceForm) -> types.RCTFInstanceDetails:
    form.check_token()
    return await instances.get_instance(form.challenge_integration_id, form.team_id)


@router.put('/')
async def start_instance(form: types.RCTFCreateInstanceForm) -> types.RCTFInstanceDetails:
    form.check_token()
    return await instances.start_instance(form)


@router.delete('/')
async def stop_instance(form: types.RCTFStopInstanceForm) -> types.RCTFInstanceDetails:
    form.check_token()
    return await instances.stop_instance(form.challenge_integration_id, form.team_id)
