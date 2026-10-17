-- Read-only owner inspection. No DDL, financial mutations or payload exports.
-- NOT a measurement of billable organization-wide network egress.
SELECT clock_timestamp() AS observed_at,
       pg_database_size(current_database()) AS total_database_bytes,
       (SELECT sum(pg_total_relation_size(c.oid))
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname='bot_private' AND c.relkind IN ('r','m')) AS private_table_index_bytes,
       (SELECT count(*) FROM bot_private.operation_data) AS data_generations,
       (SELECT coalesce(sum(pg_column_size(payload)),0)
          FROM bot_private.operation_data) AS generation_payload_bytes;

SELECT date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AS utc_day,
       count(*) FILTER (WHERE (payload->>'at')::timestamptz >=
         date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS today_reserved,
       count(*) AS month_reserved,
       count(*) FILTER (WHERE payload->>'status'='RESERVED') AS month_unresolved,
       count(*) FILTER (WHERE payload->>'status'='ERROR') AS month_errors
  FROM bot_private.records
 WHERE kind='api_usage' AND (payload->>'at')::timestamptz >=
   date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
