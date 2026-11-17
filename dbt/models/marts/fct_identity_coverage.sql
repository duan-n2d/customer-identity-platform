-- How much of each source ended up linked to other records, and how usable its identifiers are.
-- A source whose linked share collapses is the first sign that its identifiers went bad.
with r as (
    select * from {{ ref('int_record_person') }}
),

sizes as (
    select person_id, count(*) as person_records from r group by person_id
)

select
    r.source,
    count(*) as records,
    count(distinct r.person_id) as persons,
    round(avg(case when s.person_records > 1 then 1.0 else 0.0 end), 4) as share_linked,
    round(avg(case when r.phone_usable then 1.0 else 0.0 end), 4) as share_with_usable_phone,
    round(avg(case when r.email_norm is not null then 1.0 else 0.0 end), 4) as share_with_email,
    round(avg(case when r.phone_is_shared then 1.0 else 0.0 end), 4) as share_on_shared_phone
from r
inner join sizes s on s.person_id = r.person_id
group by r.source
