"""Customer lookup and erasure API.

Design rules
------------
* **No PII in logs, URLs or error messages.** Lookups are ``POST`` with a JSON body, never a
  query string (query strings end up in access logs).
* **Honest answers about ambiguity.** A phone shared by a household or a store has no single
  owner. The API says so instead of guessing.
* **Merged IDs keep working.** An old ID that was merged away returns ``301`` to the canonical
  one, so systems that stored it do not break.
* **Erasure is privileged.** It needs an admin key and is disabled when none is configured.
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Header, HTTPException, Response
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


class Person(BaseModel):
    person_id: str
    full_name: str | None
    primary_phone: str | None
    primary_phone_is_shared: bool
    primary_email: str | None
    dob: str | None
    n_records: int
    sources: list[str]


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

    @app.get("/v1/persons/{person_id}", response_model=Person)
    def get_person(person_id: str, response: Response) -> Person:
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
                response.headers["Location"] = f"/v1/persons/{target}"
                raise HTTPException(301, "merged", headers={"Location": f"/v1/persons/{target}"})
            row = wh.df("SELECT * FROM gold.dim_customer WHERE person_id = ?", [person_id])
        if row.empty:
            raise HTTPException(404, "no golden record yet; run the dbt build")
        r = row.iloc[0]

        def text(v):
            return None if v is None or v != v else str(v)

        return Person(
            person_id=r["person_id"],
            full_name=text(r["full_name"]),
            primary_phone=text(r["primary_phone"]),
            primary_phone_is_shared=bool(r["primary_phone_is_shared"]),
            primary_email=text(r["primary_email"]),
            dob=text(r["dob"]),
            n_records=int(r["n_records"]),
            sources=str(r["source_list"]).split(","),
        )

    @app.post("/v1/resolve", response_model=ResolveResponse)
    def resolve(req: ResolveRequest) -> ResolveResponse:
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
        if not x_admin_key or not hmac.compare_digest(x_admin_key, expected):
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
