import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

import aiohttp
from aiocache import cached
import requests

from open_webui.models.users import UserModel
from fastapi import Depends, HTTPException, Request, APIRouter
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from open_webui.models.models import Models
from open_webui.config import (
    CACHE_DIR,
)
from open_webui.env import (
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    ENABLE_FORWARD_USER_INFO_HEADERS,
    BYPASS_MODEL_ACCESS_CONTROL,
)

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import ENV, SRC_LOG_LEVELS

from open_webui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_model_system_prompt_to_body,
)

from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_access


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OPENAI"])




##########################################
#
# Utility functions
#
##########################################


async def send_get_request(url, key=None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url, headers={**({"Authorization": f"Bearer {key}"} if key else {})}
            ) as response:
                return await response.json()
    except Exception as e:
        # Handle connection error here
        log.error(f"Connection error: {e}")
        return None


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


##########################################
#
# API routes
#
##########################################

router = APIRouter()

# RAG_SERVER_URL = "http://localhost:8001/alfred-oi/api/query"  #

# @router.post("/alfred-oi/query")
# async def query_rag(payload: dict):
#     try:
#         response = requests.post(RAG_SERVER_URL, json=payload)
#         return response.json()
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_config(request: Request):
    return {
        "ENABLE_AIFRED_API": request.app.state.config.ENABLE_AIFRED_API,
        "AIFRED_API_BASE_URLS": request.app.state.config.AIFRED_API_BASE_URLS,
        "AIFRED_API_KEYS": request.app.state.config.AIFRED_API_KEYS,
        "AIFRED_API_CONFIGS": request.app.state.config.AIFRED_API_CONFIGS,
    }

class AifredConfigForm(BaseModel):
    ENABLE_AIFRED_API: Optional[bool] = None
    AIFRED_API_BASE_URLS: list[str]
    AIFRED_API_KEYS: list[str]
    AIFRED_API_CONFIGS: dict


@router.post("/config/update")
async def update_config(
    request: Request, form_data: AifredConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.ENABLE_AIFRED_API = form_data.ENABLE_AIFRED_API
    request.app.state.config.AIFRED_API_BASE_URLS = form_data.AIFRED_API_BASE_URLS
    request.app.state.config.AIFRED_API_KEYS = form_data.AIFRED_API_KEYS

    # Check if API KEYS length is same than API URLS length
    if len(request.app.state.config.AIFRED_API_KEYS) != len(
        request.app.state.config.AIFRED_API_BASE_URLS
    ):
        if len(request.app.state.config.AIFRED_API_KEYS) > len(
            request.app.state.config.AIFRED_API_BASE_URLS
        ):
            request.app.state.config.AIFRED_API_KEYS = (
                request.app.state.config.AIFRED_API_KEYS[
                    : len(request.app.state.config.AIFRED_API_BASE_URLS)
                ]
            )
        else:
            request.app.state.config.AIFRED_API_KEYS += [""] * (
                len(request.app.state.config.AIFRED_API_BASE_URLS)
                - len(request.app.state.config.AIFRED_API_KEYS)
            )

    request.app.state.config.AIFRED_API_CONFIGS = form_data.AIFRED_API_CONFIGS

    # Remove the API configs that are not in the API URLS
    keys = list(map(str, range(len(request.app.state.config.AIFRED_API_BASE_URLS))))
    request.app.state.config.AIFRED_API_CONFIGS = {
        key: value
        for key, value in request.app.state.config.AIFRED_API_CONFIGS.items()
        if key in keys
    }

    return {
        "ENABLE_AIFRED_API": request.app.state.config.ENABLE_AIFRED_API,
        "AIFRED_API_BASE_URLS": request.app.state.config.AIFRED_API_BASE_URLS,
        "AIFRED_API_KEYS": request.app.state.config.AIFRED_API_KEYS,
        "AIFRED_API_CONFIGS": request.app.state.config.AIFRED_API_CONFIGS,
    }


@router.post("/audio/speech")
async def speech(request: Request, user=Depends(get_verified_user)):
    idx = None
    try:
        idx = request.app.state.config.AIFRED_API_BASE_URLS.index(
            "https://api.openai.com/v1"
        )

        body = await request.body()
        name = hashlib.sha256(body).hexdigest()

        SPEECH_CACHE_DIR = Path(CACHE_DIR).joinpath("./audio/speech/")
        SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = SPEECH_CACHE_DIR.joinpath(f"{name}.mp3")
        file_body_path = SPEECH_CACHE_DIR.joinpath(f"{name}.json")

        # Check if the file already exists in the cache
        if file_path.is_file():
            return FileResponse(file_path)

        url = request.app.state.config.AIFRED_API_BASE_URLS[idx]

        r = None
        try:
            r = requests.post(
                url=f"{url}/audio/speech",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {request.app.state.config.AIFRED_API_KEYS[idx]}",
                    **(
                        {
                            "HTTP-Referer": "https://axlrator.com/",
                            "X-Title": "AXLRator",
                        }
                        if "openrouter.ai" in url
                        else {}
                    ),
                    **(
                        {
                            "X-OpenWebUI-User-Name": user.name,
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS
                        else {}
                    ),
                },
                stream=True,
            )

            r.raise_for_status()

            # Save the streaming content to a file
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

            with open(file_body_path, "w") as f:
                json.dump(json.loads(body.decode("utf-8")), f)

            # Return the saved file
            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)

            detail = None
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        detail = f"External: {res['error']}"
                except Exception:
                    detail = f"External: {e}"

            raise HTTPException(
                status_code=r.status_code if r else 500,
                detail=detail if detail else "Open WebUI: Server Connection Error",
            )

    except ValueError:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.OPENAI_NOT_FOUND)


async def get_all_models_responses(request: Request) -> list:
    if not request.app.state.config.ENABLE_AIFRED_API:
        return []

    # Check if API KEYS length is same than API URLS length
    num_urls = len(request.app.state.config.AIFRED_API_BASE_URLS)
    num_keys = len(request.app.state.config.AIFRED_API_KEYS)

    if num_keys != num_urls:
        # if there are more keys than urls, remove the extra keys
        if num_keys > num_urls:
            new_keys = request.app.state.config.AIFRED_API_KEYS[:num_urls]
            request.app.state.config.AIFRED_API_KEYS = new_keys
        # if there are more urls than keys, add empty keys
        else:
            request.app.state.config.AIFRED_API_KEYS += [""] * (num_urls - num_keys)

    request_tasks = []
    for idx, url in enumerate(request.app.state.config.AIFRED_API_BASE_URLS):
        if (str(idx) not in request.app.state.config.AIFRED_API_CONFIGS) and (
            url not in request.app.state.config.AIFRED_API_CONFIGS  # Legacy support
        ):
            request_tasks.append(
                send_get_request(
                    f"{url}/models", request.app.state.config.AIFRED_API_KEYS[idx]
                )
            )
        else:
            api_config = request.app.state.config.AIFRED_API_CONFIGS.get(
                str(idx),
                request.app.state.config.AIFRED_API_CONFIGS.get(
                    url, {}
                ),  # Legacy support
            )

            enable = api_config.get("enable", True)
            model_ids = api_config.get("model_ids", [])

            if enable:
                if len(model_ids) == 0:
                    request_tasks.append(
                        send_get_request(
                            f"{url}/models",
                            request.app.state.config.AIFRED_API_KEYS[idx],
                        )
                    )
                else:
                    model_list = {
                        "object": "list",
                        "data": [
                            {
                                "id": model_id,
                                "name": model_id,
                                "owned_by": "aifred",
                                "aifred": {"id": model_id},
                                "urlIdx": idx,
                            }
                            for model_id in model_ids
                        ],
                    }

                    request_tasks.append(
                        asyncio.ensure_future(asyncio.sleep(0, model_list))
                    )
            else:
                request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

    responses = await asyncio.gather(*request_tasks)

    for idx, response in enumerate(responses):
        if response:
            url = request.app.state.config.AIFRED_API_BASE_URLS[idx]
            api_config = request.app.state.config.AIFRED_API_CONFIGS.get(
                str(idx),
                request.app.state.config.AIFRED_API_CONFIGS.get(
                    url, {}
                ),  # Legacy support
            )

            prefix_id = api_config.get("prefix_id", None)

            if prefix_id:
                for model in (
                    response if isinstance(response, list) else response.get("data", [])
                ):
                    model["id"] = f"{prefix_id}.{model['id']}"

    log.debug(f"get_all_models:responses() {responses}")
    return responses


async def get_filtered_models(models, user):
    # Filter models based on user access control
    filtered_models = []
    for model in models.get("data", []):
        model_info = Models.get_model_by_id(model["id"])
        if model_info:
            if user.id == model_info.user_id or has_access(
                user.id, type="read", access_control=model_info.access_control
            ):
                filtered_models.append(model)
    return filtered_models


@cached(ttl=3)
async def get_all_models(request: Request, user: UserModel = None) -> dict[str, list]:
    log.info("get_all_models()")

    if not request.app.state.config.ENABLE_AIFRED_API:
        return {"data": []}

    responses = await get_all_models_responses(request)

    def extract_data(response):
        if response and "data" in response:
            return response["data"]
        if isinstance(response, list):
            return response
        return None

    def merge_models_lists(model_lists):
        log.debug(f"merge_models_lists {model_lists}")
        merged_list = []

        for idx, models in enumerate(model_lists):
            if models is not None and "error" not in models:
                merged_list.extend(
                    [
                        {
                            **model,
                            "name": model.get("name", model["id"]),
                            "owned_by": "aifred",
                            "aifred": model,
                            "urlIdx": idx,
                        }
                        for model in models
                        if "api.aifred.com"
                        not in request.app.state.config.AIFRED_API_BASE_URLS[idx]
                        or not any(
                            name in model["id"]
                            for name in [
                                "babbage",
                                "dall-e",
                                "davinci",
                                "embedding",
                                "tts",
                                "whisper",
                            ]
                        )
                    ]
                )

        return merged_list

    models = {"data": merge_models_lists(map(extract_data, responses))}
    log.debug(f"models: {models}")

    request.app.state.AIFRED_MODELS = {model["id"]: model for model in models["data"]}
    return models


@router.get("/models")
@router.get("/models/{url_idx}")
async def get_models(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
    log.debug(f"--------------------------------/models--------{url_idx} ----------------- {request}")
    # models = {
    #     "data": [],
    # }    
    models = {
        "object": "list",
        "data": [
            {
            "id": "model-id-0",
            "object": "model",
            "created": 1686935002,
            "owned_by": "organization-owner"
            },
            {
            "id": "model-id-1",
            "object": "model",
            "created": 1686935002,
            "owned_by": "organization-owner",
            },
            {
            "id": "model-id-2",
            "object": "model",
            "created": 1686935002,
            "owned_by": "openai"
            },
        ],
        "object": "list"
    }

    return models

async def getSources(request: Request, user=Depends(get_verified_user)):
    idx = 0
    try:
        # idx = request.app.state.config.AIFRED_API_BASE_URLS.index(
        #     "https://api.openai.com/v1"
        # )
        # AXL:김정민: 오류나서 idx 0으로 고정

        body = await request.body()
                        
        url = request.app.state.config.AIFRED_API_BASE_URLS[idx]

        r = None
        try:
            r = requests.post(
                url=f"{url}/chat/completed",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {request.app.state.config.AIFRED_API_KEYS[idx]}",
                    **(
                        {
                            "HTTP-Referer": "https://axlrator.com/",
                            "X-Title": "AXLRator",
                        }
                        if "openrouter.ai" in url
                        else {}
                    ),
                    **(
                        {
                            "X-OpenWebUI-User-Name": user.name,
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS
                        else {}
                    ),
                },
                stream=True,
            )
        
            response = r.json()
        except Exception as e:
            log.error(e)
            response = await r.text()
            log.error(f"Error response: {response}")

        r.raise_for_status()
        return response
    except Exception as e:
        log.exception(e)

        detail = None
        if isinstance(response, dict):
            if "error" in response:
                detail = f"{response['error']['message'] if 'message' in response['error'] else response['error']}"
        elif isinstance(response, str):
            detail = response

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Open WebUI: Server Connection Error",
        )
    finally:        
        if r:
            r.close()
        
    

class ConnectionVerificationForm(BaseModel):
    url: str
    key: str


@router.post("/verify")
async def verify_connection(
    form_data: ConnectionVerificationForm, user=Depends(get_admin_user)
):
    url = form_data.url
    key = form_data.key

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    ) as session:
        try:
            async with session.get(
                f"{url}/models",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            ) as r:
                if r.status != 200:
                    # Extract response error details if available
                    error_detail = f"HTTP Error: {r.status}"
                    res = await r.json()
                    if "error" in res:
                        error_detail = f"External Error: {res['error']}"
                    raise Exception(error_detail)

                response_data = await r.json()
                return response_data

        except aiohttp.ClientError as e:
            # ClientError covers all aiohttp requests issues
            log.exception(f"Client error: {str(e)}")
            raise HTTPException(
                status_code=500, detail="Open WebUI: Server Connection Error"
            )
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            error_detail = f"Unexpected error: {str(e)}"
            raise HTTPException(status_code=500, detail=error_detail)


@router.post("/chat/completions")
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    bypass_filter: Optional[bool] = False,
):
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    idx = 0

    payload = {**form_data}
    #metadata = payload.pop("metadata", None)
    metadata = payload.get("metadata", None) #AXL:김정민 20250708 metadata 없애지 않고 살리기

    model_id = form_data.get("model")
    model_info = Models.get_model_by_id(model_id)

    # Check model info and override the payload
    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id
            model_id = model_info.base_model_id

        params = model_info.params.model_dump()
        payload = apply_model_params_to_body_openai(params, payload) #TODO aifred용 함수 추가 하고 해당 라인 교체
        payload = apply_model_system_prompt_to_body(params, payload, metadata, user)

        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            if not (
                user.id == model_info.user_id
                or has_access(
                    user.id, type="read", access_control=model_info.access_control
                )
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Model not found",
                )
    elif not bypass_filter:
        if user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Model not found",
            )

    await get_all_models(request)
    model = request.app.state.AIFRED_MODELS.get(model_id)
    if model:
        idx = model["urlIdx"]
    else:
        raise HTTPException(
            status_code=404,
            detail="Model not found",
        )

    # Get the API config for the model
    api_config = request.app.state.config.AIFRED_API_CONFIGS.get(
        str(idx),
        request.app.state.config.AIFRED_API_CONFIGS.get(
            request.app.state.config.AIFRED_API_BASE_URLS[idx], {}
        ),  # Legacy support
    )

    prefix_id = api_config.get("prefix_id", None)
    if prefix_id:
        payload["model"] = payload["model"].replace(f"{prefix_id}.", "")

    # Add user info to the payload if the model is a pipeline
    if "pipeline" in model and model.get("pipeline"):
        payload["user"] = {
            "name": user.name,
            "id": user.id,
            "email": user.email,
            "role": user.role,
        }

    url = request.app.state.config.AIFRED_API_BASE_URLS[idx]
    key = request.app.state.config.AIFRED_API_KEYS[idx]

    # Convert the modified body back to JSON
    payload = json.dumps(payload)
    print(f"###payload: {payload}")

    r = None
    session = None
    streaming = False
    response = None

    try:
        session = aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        )

        r = await session.request(
            method="POST",
            url=f"{url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **(
                    {
                        "HTTP-Referer": "https://axlrator.com/",
                        "X-Title": "AXLRator",
                    }
                    if "openrouter.ai" in url
                    else {}
                ),
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS
                    else {}
                ),
            },
        )

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            try:
                response = await r.json()
            except Exception as e:
                log.error(e)
                response = await r.text()

            r.raise_for_status()
            return response
    except Exception as e:
        log.exception(e)

        detail = None
        if isinstance(response, dict):
            if "error" in response:
                detail = f"{response['error']['message'] if 'message' in response['error'] else response['error']}"
        elif isinstance(response, str):
            detail = response

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Open WebUI: Server Connection Error",
        )
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request, user=Depends(get_verified_user)):
    """
    Deprecated: proxy all requests to OpenAI API
    """

    body = await request.body()

    idx = 0
    url = request.app.state.config.AIFRED_API_BASE_URLS[idx]
    key = request.app.state.config.AIFRED_API_KEYS[idx]

    r = None
    session = None
    streaming = False

    try:
        session = aiohttp.ClientSession(trust_env=True)
        r = await session.request(
            method=request.method,
            url=f"{url}/{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS
                    else {}
                ),
            },
        )
        r.raise_for_status()

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            response_data = await r.json()
            return response_data

    except Exception as e:
        log.exception(e)

        detail = None
        if r is not None:
            try:
                res = await r.json()
                print(res)
                if "error" in res:
                    detail = f"External: {res['error']['message'] if 'message' in res['error'] else res['error']}"
            except Exception:
                detail = f"External: {e}"
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Open WebUI: Server Connection Error",
        )
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()
