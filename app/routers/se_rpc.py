"""The RPC console: every documented method, callable raw.

The REST resources cover the API's whole surface in a shaped way; this is the
unshaped way, for the operator who reads the SoftEther API reference and wants
to try something exactly. It is also the guarantee that nothing the server can
do is out of the panel's reach -- including the two VPN-Gate-only methods the
reference document names but does not describe.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..audit import record
from ..config import APP_ROOT
from ..deps import CurrentUser
from ..se import rpc

router = APIRouter(prefix="/rpc", tags=["softether-rpc"])

# Methods the reference lists without a body; callable, with no parameter docs.
_UNDOCUMENTED = [
    {"name": "GetVgsConfig", "title": "Get the VPN Gate Service configuration", "desc": "VPN Gate builds only; a stock server answers with an error.", "params": [], "input": {}},
    {"name": "SetVgsConfig", "title": "Set the VPN Gate Service configuration", "desc": "VPN Gate builds only; a stock server answers with an error.", "params": [], "input": {}},
]


@lru_cache
def _spec() -> list[dict[str, Any]]:
    path = APP_ROOT / "Library" / "spec.json"
    with open(path, encoding="utf-8") as handle:
        return json.load(handle) + _UNDOCUMENTED


@lru_cache
def _method_names() -> frozenset[str]:
    return frozenset(m["name"] for m in _spec())


@router.get("/methods")
def list_methods(user: dict = CurrentUser) -> list[dict[str, Any]]:
    return [
        {
            "name": m["name"],
            "title": m.get("title", ""),
            "desc": m.get("desc", ""),
            "input": m.get("input", {}),
            "params": m.get("params", []),
        }
        for m in _spec()
    ]


class RpcCall(BaseModel):
    method: str = Field(min_length=1, max_length=64)
    params: Optional[dict[str, Any]] = None


@router.post("/call")
def call_method(body: RpcCall, user: dict = CurrentUser) -> dict[str, Any]:
    if body.method not in _method_names():
        raise HTTPException(status_code=422, detail=f"{body.method} is not a documented RPC method.")
    result = rpc(body.method, body.params or {})
    record(user, "rpc.called", "server", "", body.method)
    return result
