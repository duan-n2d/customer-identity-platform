-- Analytics-safe view of the customer: no phone, email, name or date of birth.
-- Hashing a phone number does not make it safe (there are only ~10^9 possible numbers, so a plain
-- hash can be reversed by enumeration), so direct identifiers are not exposed at all. Analysts get
-- coarse attributes; joins use person_id.
select
    person_id,
    case
        when dob is null then 'unknown'
        when date_diff('year', cast(dob as date), date '{{ var("as_of_date") }}') < 25 then 'under_25'
        when date_diff('year', cast(dob as date), date '{{ var("as_of_date") }}') < 35 then '25_34'
        when date_diff('year', cast(dob as date), date '{{ var("as_of_date") }}') < 45 then '35_44'
        when date_diff('year', cast(dob as date), date '{{ var("as_of_date") }}') < 55 then '45_54'
        else '55_plus'
    end as age_band,
    primary_phone is not null as has_phone,
    primary_email is not null as has_email,
    primary_phone_is_shared as shares_phone_with_others,
    n_sources,
    n_records,
    n_sources > 1 as is_multi_source,
    strftime(first_seen, '%Y-%m') as first_seen_month
from {{ ref('dim_customer') }}
