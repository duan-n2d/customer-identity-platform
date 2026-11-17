-- Each record with its person_id and flags describing how trustworthy its identifiers are.
with records as (
    select * from {{ ref('stg_records') }}
),

person_map as (
    select record_key, person_id from {{ source('resolve', 'person_map') }}
),

hubs as (
    select identifier from {{ source('resolve', 'hub_identifiers') }}
),

shared as (
    select identifier from {{ source('resolve', 'shared_identifiers') }}
),

trust as (
    select source, trust from {{ ref('source_trust') }}
)

select
    pm.person_id,
    r.record_key,
    r.source,
    r.event_date,
    r.name_raw,
    r.name_key,
    r.email_norm,
    r.phone_e164,
    r.dob_norm,
    t.trust as source_trust,
    -- a name written with diacritics is the better rendering of the same name
    (r.name_raw is not null and regexp_matches(r.name_raw, '[^\x00-\x7F]')) as name_has_diacritics,
    -- ALL-CAPS is how a cashier types it; the mixed-case rendering is the better one
    (r.name_raw is not null and r.name_raw = upper(r.name_raw)) as name_is_upper,
    -- a hub phone (a store's own number) must never become anyone's primary phone
    (r.phone_e164 is not null and r.phone_e164 not in (select identifier from hubs)) as phone_usable,
    (r.phone_e164 is not null and r.phone_e164 in (select identifier from shared)) as phone_is_shared
from records r
inner join person_map pm on pm.record_key = r.record_key
left join trust t on t.source = r.source
