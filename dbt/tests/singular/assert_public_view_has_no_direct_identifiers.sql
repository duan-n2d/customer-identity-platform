-- The analytics-safe view must not expose a column that looks like a direct identifier.
-- If someone adds `phone`, `email`, `name` or `dob` to it, this test fails the build.
select column_name
from information_schema.columns
where table_schema = '{{ target.schema }}'
  and table_name = 'dim_customer_public'
  and regexp_matches(column_name, '(phone|email|name|dob)')
  and column_name not in ('has_phone', 'has_email', 'shares_phone_with_others')
