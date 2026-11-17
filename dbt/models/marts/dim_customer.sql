-- Golden record: one row per person, each attribute chosen by an explicit survivorship rule.
--
--   full_name      diacritics first, then mixed case over ALL CAPS, then the most trusted source
--   primary_phone  the most recent usable (non-hub) phone, then the most trusted source
--   primary_email  the most recent email, then the most trusted source
--   dob            the most common date of birth, then the most trusted source
--
-- Rules are window functions on purpose: they are plain SQL, readable in a code review, and
-- run unchanged on Databricks.
with r as (
    select * from {{ ref('int_record_person') }}
),

name_candidates as (
    select
        person_id,
        name_raw,
        bool_or(name_has_diacritics) as has_diacritics,
        bool_or(name_is_upper) as is_upper,
        max(source_trust) as best_trust,
        count(*) as n,
        max(event_date) as last_seen
    from r
    where name_key is not null  -- a one-word name is not a usable name
    group by person_id, name_raw
),

names as (
    select
        person_id,
        name_raw as full_name,
        row_number() over (
            partition by person_id
            order by has_diacritics desc, is_upper asc, best_trust desc, n desc, last_seen desc, name_raw
        ) as rn
    from name_candidates
),

phone_candidates as (
    select
        person_id,
        phone_e164,
        bool_or(phone_is_shared) as is_shared,
        max(source_trust) as best_trust,
        count(*) as n,
        max(event_date) as last_seen
    from r
    where phone_usable
    group by person_id, phone_e164
),

phones as (
    select
        person_id,
        phone_e164 as primary_phone,
        is_shared as primary_phone_is_shared,
        row_number() over (
            partition by person_id
            order by last_seen desc, best_trust desc, n desc, phone_e164
        ) as rn
    from phone_candidates
),

email_candidates as (
    select
        person_id,
        email_norm,
        max(source_trust) as best_trust,
        count(*) as n,
        max(event_date) as last_seen
    from r
    where email_norm is not null
    group by person_id, email_norm
),

emails as (
    select
        person_id,
        email_norm as primary_email,
        row_number() over (
            partition by person_id
            order by last_seen desc, best_trust desc, n desc, email_norm
        ) as rn
    from email_candidates
),

dob_candidates as (
    select
        person_id,
        dob_norm,
        max(source_trust) as best_trust,
        count(*) as n
    from r
    where dob_norm is not null
    group by person_id, dob_norm
),

dobs as (
    select
        person_id,
        dob_norm as dob,
        row_number() over (
            partition by person_id
            order by n desc, best_trust desc, dob_norm
        ) as rn
    from dob_candidates
),

summary as (
    select
        person_id,
        count(*) as n_records,
        count(distinct source) as n_sources,
        string_agg(distinct source, ',' order by source) as source_list,
        min(event_date) as first_seen,
        max(event_date) as last_seen,
        count(distinct phone_e164) filter (where phone_usable) as n_phones
    from r
    group by person_id
)

select
    s.person_id,
    n.full_name,
    p.primary_phone,
    coalesce(p.primary_phone_is_shared, false) as primary_phone_is_shared,
    e.primary_email,
    d.dob,
    s.n_records,
    s.n_sources,
    s.source_list,
    s.n_phones,
    s.first_seen,
    s.last_seen
from summary s
left join names n on n.person_id = s.person_id and n.rn = 1
left join phones p on p.person_id = s.person_id and p.rn = 1
left join emails e on e.person_id = s.person_id and e.rn = 1
left join dobs d on d.person_id = s.person_id and d.rn = 1
