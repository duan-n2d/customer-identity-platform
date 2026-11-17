-- A store's own phone must never be a customer's primary phone. Returns the violations.
select d.person_id, d.primary_phone
from {{ ref('dim_customer') }} d
inner join {{ source('resolve', 'hub_identifiers') }} h
    on h.identifier = d.primary_phone
