"""Customer lookup and erasure API.

Design rules
------------
* **No PII in logs, URLs or error messages.** Lookups are ``POST`` with a JSON body, never a
  query string (query strings end up in access logs).
* **Honest answers about ambiguity.** A phone shared by a household or a store has no single
  owner. The API says so instead of guessing.
* **Merged IDs keep working.** An old ID that was merged away returns ``301`` to the canonical
  one, so systems that stored it do not break.
* **Fail closed.** The lookup endpoints need an ``X-API-Key`` matching ``CIP_API_KEY``; with no
  key configured they answer ``503`` instead of serving anyone. ``/health`` stays open.
* **Public by default.** Person lookups return the analytics-safe ``gold.dim_customer_public``
  row (no name, phone, email or date of birth). The full record (``?view=pii``) and the detailed
  ``/v1/resolve`` reasons (hub / shared number, candidate counts) also need an ``X-PII-Key``
  matching ``CIP_API_PII_KEY``; with that unset, PII is never served.
* **Erasure is privileged.** It needs an admin key and is disabled when none is configured.

Person IDs are sequential (``P00000001``), so they are guessable. Requiring a key for every
lookup, and keeping direct identifiers behind a second key, is what stops enumeration from
harvesting PII; making IDs opaque would change the ID stability contract and is left for later.
"""

from __future__ import annotations

import os
import secrets
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from cip import privacy
from cip.config import Settings
from cip.normalize import normalize_email, normalize_phone
from cip.resolve.ids import canonical
from cip.warehouse import Warehouse


class ResolveRequest(BaseModel):
    phone: str | None = Field(default=None, description="Any common Vietnamese phone format")
    email: str | None = None


class ResolveResponse(BaseModel):
    person_id: str | None
    basis: str | None = None
    reason: str | None = None
    candidates: int = 0


class PersonPublic(BaseModel):
    """The ``gold.dim_customer_public`` row: coarse attributes, no direct identifiers."""

    person_id: str
    age_band: str
    has_phone: bool
    has_email: bool
    shares_phone_with_others: bool
    n_sources: int
    n_records: int
    is_multi_source: bool
    first_seen_month: str | None


class Person(BaseModel):
    """The full golden record. Served only with ``view=pii`` and a valid PII key."""

    person_id: str
    full_name: str | None
    primary_phone: str | None
    primary_phone_is_shared: bool
    primary_email: str | None
    dob: str | None
    n_records: int
    sources: list[str]


Scope = Literal["public", "pii"]


def _key_matches(given: str | None, env_var: str) -> bool:
    expected = os.environ.get(env_var)
    if not expected or not given:
        return False
    return secrets.compare_digest(given.encode(), expected.encode())


def api_scope(
    x_api_key: str | None = Header(default=None),
    x_pii_key: str | None = Header(default=None),
) -> Scope:
    """Authenticate the caller and return what they may see. Fails closed."""
    if not os.environ.get("CIP_API_KEY"):
        raise HTTPException(503, "the API is disabled: CIP_API_KEY is not configured")
    if not _key_matches(x_api_key, "CIP_API_KEY"):
        raise HTTPException(
            401, "missing or invalid X-API-Key", headers={"WWW-Authenticate": "ApiKey"}
        )
    return "pii" if _key_matches(x_pii_key, "CIP_API_PII_KEY") else "public"


ApiScope = Annotated[Scope, Depends(api_scope)]


def _text(v) -> str | None:
    return None if v is None or v != v else str(v)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Customer Identity API", version="0.1.0")

    def db() -> Warehouse:
        return Warehouse(settings, read_only=True)

    @app.get("/health")
    def health() -> dict:
        with db() as wh:
            n = wh.df("SELECT count(*) AS n FROM gold.dim_customer")["n"][0]
        return {"status": "ok", "persons": int(n)}

    @app.get("/v1/persons/{person_id}", response_model=PersonPublic | Person)
    def get_person(
        person_id: str,
        response: Response,
        scope: ApiScope,
        view: Literal["public", "pii"] = Query(default="public"),
    ) -> PersonPublic | Person:
        if view == "pii" and scope != "pii":
            raise HTTPException(403, "view=pii needs a valid X-PII-Key")
        with db() as wh:
            registry = wh.df("SELECT person_id, status, merged_into FROM resolve.person_registry")
            known = registry[registry["person_id"] == person_id]
            if known.empty:
                raise HTTPException(404, "unknown person")
            status = known.iloc[0]["status"]
            if status == "erased":
                raise HTTPException(410, "this person was erased at their request")
            if status == "merged":
                target = canonical(registry, person_id)
                location = f"/v1/persons/{target}" + ("?view=pii" if view == "pii" else "")
                response.headers["Location"] = location
                raise HTTPException(301, "merged", headers={"Location": location})
            table = "gold.dim_customer" if view == "pii" else "gold.dim_customer_public"
            row = wh.df(f"SELECT * FROM {table} WHERE person_id = ?", [person_id])
        if row.empty:
            raise HTTPException(404, "no golden record yet; run the dbt build")
        r = row.iloc[0]

        if view == "public":
            return PersonPublic(
                person_id=r["person_id"],
                age_band=str(r["age_band"]),
                has_phone=bool(r["has_phone"]),
                has_email=bool(r["has_email"]),
                shares_phone_with_others=bool(r["shares_phone_with_others"]),
                n_sources=int(r["n_sources"]),
                n_records=int(r["n_records"]),
                is_multi_source=bool(r["is_multi_source"]),
                first_seen_month=_text(r["first_seen_month"]),
            )
        return Person(
            person_id=r["person_id"],
            full_name=_text(r["full_name"]),
            primary_phone=_text(r["primary_phone"]),
            primary_phone_is_shared=bool(r["primary_phone_is_shared"]),
            primary_email=_text(r["primary_email"]),
            dob=_text(r["dob"]),
            n_records=int(r["n_records"]),
            sources=str(r["source_list"]).split(","),
        )

    @app.post("/v1/resolve", response_model=ResolveResponse)
    def resolve(req: ResolveRequest, scope: ApiScope) -> ResolveResponse:
        result = _resolve(req)
        if scope == "pii" or result.person_id or result.reason == "not_found":
            return result
        # Without the PII scope, do not say whether a number is a store hub or shared by a
        # household, nor how many people use it: that is membership information.
        return ResolveResponse(person_id=None, basis=result.basis, reason="no_unique_match")

    def _resolve(req: ResolveRequest) -> ResolveResponse:
        phone = normalize_phone(req.phone).e164 if req.phone else None
        email = normalize_email(req.email) if req.email else None
        if not phone and not email:
            raise HTTPException(422, "provide a valid phone or email")
        with db() as wh:
            if phone:
                if wh.df(
                    "SELECT 1 FROM resolve.hub_identifiers WHERE identifier = ?", [phone]
                ).shape[0]:
                    return ResolveResponse(person_id=None, reason="hub_identifier", basis="phone")
                shared = wh.df(
                    "SELECT 1 FROM resolve.shared_identifiers WHERE identifier = ?", [phone]
                )
                found = wh.df(
                    "SELECT DISTINCT pm.person_id FROM stg.records r "
                    "JOIN resolve.person_map pm USING (record_key) WHERE r.phone_e164 = ?",
                    [phone],
                )
                if shared.shape[0]:
                    return ResolveResponse(
                        person_id=None,
                        basis="phone",
                        reason="shared_identifier",
                        candidates=len(found),
                    )
                if len(found) == 1:
                    return ResolveResponse(person_id=found["person_id"][0], basis="phone")
                if len(found) > 1:
                    return ResolveResponse(
                        person_id=None, basis="phone", reason="ambiguous", candidates=len(found)
                    )
            if email:
                found = wh.df(
                    "SELECT DISTINCT pm.person_id FROM stg.records r "
                    "JOIN resolve.person_map pm USING (record_key) WHERE r.email_norm = ?",
                    [email],
                )
                if len(found) == 1:
                    return ResolveResponse(person_id=found["person_id"][0], basis="email")
                if len(found) > 1:
                    return ResolveResponse(
                        person_id=None, basis="email", reason="ambiguous", candidates=len(found)
                    )
        return ResolveResponse(person_id=None, reason="not_found")

    @app.delete("/v1/subjects/{person_id}")
    def erase(person_id: str, x_admin_key: str | None = Header(default=None)) -> dict:
        expected = os.environ.get("CIP_ADMIN_KEY")
        if not expected:
            raise HTTPException(503, "erasure is disabled: CIP_ADMIN_KEY is not configured")
        if not _key_matches(x_admin_key, "CIP_ADMIN_KEY"):
            raise HTTPException(403, "forbidden")
        summary = privacy.erase_person(settings, person_id)
        if not summary["found"]:
            raise HTTPException(404, "unknown person")
        residue = privacy.verify_erasure(settings, person_id, summary["record_keys"])
        return {
            "person_id": person_id,
            "erased_records": summary["erased_records"],
            "residue": residue,
            "verified": all(v == 0 for v in residue.values()),
        }

    return app
