-- One row per source record, as the pipeline normalised it.
select
    record_key,
    source,
    source_record_id,
    event_date,
    name_raw,
    name_key,
    email_norm,
    phone_e164,
    dob_norm
from {{ source('stg', 'records') }}
