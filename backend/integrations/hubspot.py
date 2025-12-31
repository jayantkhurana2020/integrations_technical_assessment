import json
from fastapi.responses import HTMLResponse
import requests
import httpx
import base64
import secrets
import asyncio
from urllib.parse import urlencode, quote
from datetime import datetime, timedelta
from fastapi import Request
from integrations.integration_item import IntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

HUBSPOT_CLIENT_ID = "833d138e-1899-4b8d-ac21-233acab33bb4"
HUBSPOT_CLIENT_SECRET = "eca14a01-a8c0-43b0-af8c-8ca11dcdaff6"
HUBSPOT_REDIRECT_URI = "http://localhost:8000/integrations/hubspot/oauth2callback"
HUBSPOT_SCOPES = "oauth crm.objects.contacts.read"


async def authorize_hubspot(user_id, org_id):
    base_url = "https://app.hubspot.com/oauth/authorize"
    state = f"{user_id}:{org_id}"

    query_params = {
        "client_id": HUBSPOT_CLIENT_ID,
        "redirect_uri": HUBSPOT_REDIRECT_URI,
        "scope": HUBSPOT_SCOPES,
        "state": state,
    }

    authorization_url = f"{base_url}?{urlencode(query_params, quote_via=quote)}"
    return {authorization_url}

async def oauth2callback_hubspot(request: Request):
    params = request.query_params
    code = params.get("code")
    state = params.get("state")
    error = params.get("error")

    if error:
        return {"error": error, "message": "User denied or OAuth error occurred."}

    if not code:
        return {"error": "missing_code", "message": "No authorization code received."}
    if state:
        user_id, org_id = state.split(":")
        await delete_key_redis(f"{user_id}:{org_id}")

    token_url = "https://api.hubapi.com/oauth/v1/token"

    payload = {
        "grant_type": "authorization_code",
        "client_id": HUBSPOT_CLIENT_ID,
        "client_secret": HUBSPOT_CLIENT_SECRET,
        "redirect_uri": HUBSPOT_REDIRECT_URI,
        "code": code,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if response.status_code != 200:
            return {"error": "token_request_failed", "details": response.text}

        token_data = response.json()

    # Extract user/org IDs from state
    user_id, org_id = state.split(":")
    key = f"{user_id}:{org_id}"

    token_data_to_store = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": (datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 1800))).isoformat(),
    }

    # 1-hour expiration
    await add_key_value_redis(key, json.dumps(token_data_to_store), expire=3600)

    # return {
    #     "message": "HubSpot OAuth successful!",
    #     "state": state,
    #     "access_token": token_data.get("access_token"),
    #     "refresh_token": token_data.get("refresh_token"),
    #     "expires_in": token_data.get("expires_in"),
    # }
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    key = f"{user_id}:{org_id}"
    stored_creds = await get_value_redis(key)

    if not stored_creds:
        return {"error": "no_credentials_found"}

    creds = json.loads(stored_creds)

    expires_at = creds.get("expires_at")
    if expires_at and datetime.utcnow() > datetime.fromisoformat(expires_at):
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            return {"error": "missing_refresh_token"}

        async with httpx.AsyncClient() as client:
            refresh_url = "https://api.hubapi.com/oauth/v1/token"
            payload = {
                "grant_type": "refresh_token",
                "client_id": HUBSPOT_CLIENT_ID,
                "client_secret": HUBSPOT_CLIENT_SECRET,
                "refresh_token": refresh_token,
            }
            res = await client.post(refresh_url, data=payload)
            if res.status_code != 200:
                return {"error": "refresh_failed", "details": res.text}

            new_data = res.json()
            creds["access_token"] = new_data["access_token"]
            creds["expires_at"] = (datetime.utcnow() + timedelta(seconds=new_data["expires_in"])).isoformat()

            # Update Redis
            await add_key_value_redis(key, json.dumps(creds), expire=3600)

    return creds

async def create_integration_item_metadata_object(response_json):
    items = []
    results = response_json.get("results", [])
    for obj in results:
        props = obj.get("properties", {})
        item = IntegrationItem(
            id=obj.get("id"),
            name=f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
            type="Contact",
            creation_time=props.get("createdate"),
            parent_path_or_name="HubSpot Contacts",
            visibility=True,
        )
        items.append(item.__dict__)  
    return items

async def get_items_hubspot(credentials):
    if isinstance(credentials, str):
        try:
            credentials = json.loads(credentials)
        except json.JSONDecodeError:
            return {"error": "Invalid credentials format"}

    access_token = credentials.get("access_token")
    if not access_token:
        return {"error": "missing_access_token"}

    endpoint = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(endpoint, headers=headers)
        if response.status_code != 200:
            return {"error": "hubspot_api_failed", "details": response.text}

        data = response.json()

    normalized_items = await create_integration_item_metadata_object(data)
    return normalized_items