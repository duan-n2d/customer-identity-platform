-- Lineage: which source records make up each person. Use it to explain any golden-record value.
select
    person_id,
    record_key,
    source,
    event_date
from {{ ref('int_record_person') }}
