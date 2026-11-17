-- Every person that owns records in the matching output must appear in the golden table.
select distinct b.person_id
from {{ ref('bridge_person_record') }} b
left join {{ ref('dim_customer') }} d on d.person_id = b.person_id
where d.person_id is null
