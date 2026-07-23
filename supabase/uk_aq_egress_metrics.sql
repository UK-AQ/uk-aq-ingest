-- UK AQ endpoint egress metrics (hybrid model)
-- 1) Minute aggregates in uk_aq_raw.endpoint_egress_metrics_minute
-- 2) Raw events only for status=304 and status>=400 in uk_aq_raw.endpoint_egress_events
-- 3) Retention helper RPC for scheduled/probabilistic cleanup

create table if not exists uk_aq_raw.endpoint_egress_metrics_minute (
  bucket_minute timestamptz not null,
  endpoint text not null,
  method text not null,
  status_class text not null check (status_class in ('2xx', '3xx', '4xx', '5xx', 'other')),
  observed_requests bigint not null default 0,
  estimated_requests numeric(18,4) not null default 0,
  response_bytes_sum bigint not null default 0,
  response_bytes_max integer not null default 0,
  duration_ms_sum bigint not null default 0,
  duration_ms_max integer not null default 0,
  updated_at timestamptz not null default now(),
  primary key (bucket_minute, endpoint, method, status_class)
);

create index if not exists endpoint_egress_metrics_minute_endpoint_idx
  on uk_aq_raw.endpoint_egress_metrics_minute (endpoint, bucket_minute desc);

create table if not exists uk_aq_raw.endpoint_egress_events (
  id bigint generated always as identity primary key,
  occurred_at timestamptz not null default now(),
  endpoint text not null,
  method text not null,
  status integer not null,
  status_class text not null check (status_class in ('2xx', '3xx', '4xx', '5xx', 'other')),
  duration_ms integer null,
  response_bytes integer null,
  sample_rate numeric(10,6) null,
  request_meta jsonb not null default '{}'::jsonb
);

create index if not exists endpoint_egress_events_occurred_at_idx
  on uk_aq_raw.endpoint_egress_events (occurred_at desc);

create index if not exists endpoint_egress_events_endpoint_idx
  on uk_aq_raw.endpoint_egress_events (endpoint, occurred_at desc);

create or replace function uk_aq_public.uk_aq_record_endpoint_metric(
  p_endpoint text,
  p_method text,
  p_status integer,
  p_duration_ms integer,
  p_response_bytes integer,
  p_sample_rate double precision default 1,
  p_occurred_at timestamptz default now(),
  p_request_meta jsonb default '{}'::jsonb
)
returns void
language plpgsql
security definer
set search_path = uk_aq_raw, uk_aq_public, public, pg_catalog
as $$
declare
  v_bucket timestamptz;
  v_status integer;
  v_status_class text;
  v_sample_rate double precision;
  v_estimated_requests numeric(18,4);
  v_duration_ms integer;
  v_response_bytes integer;
begin
  if p_endpoint is null or btrim(p_endpoint) = '' then
    raise exception 'p_endpoint is required';
  end if;
  if p_method is null or btrim(p_method) = '' then
    raise exception 'p_method is required';
  end if;

  v_status := coalesce(p_status, 0);
  v_status_class := case
    when v_status between 200 and 299 then '2xx'
    when v_status between 300 and 399 then '3xx'
    when v_status between 400 and 499 then '4xx'
    when v_status between 500 and 599 then '5xx'
    else 'other'
  end;
  v_sample_rate := greatest(0::double precision, least(coalesce(p_sample_rate, 1), 1));
  v_duration_ms := greatest(0, coalesce(p_duration_ms, 0));
  v_response_bytes := greatest(0, coalesce(p_response_bytes, 0));
  v_bucket := date_trunc('minute', coalesce(p_occurred_at, now()));

  if v_status between 200 and 299 and v_sample_rate > 0 then
    v_estimated_requests := round((1 / v_sample_rate)::numeric, 4);
  else
    v_estimated_requests := 1::numeric;
  end if;

  insert into uk_aq_raw.endpoint_egress_metrics_minute (
    bucket_minute,
    endpoint,
    method,
    status_class,
    observed_requests,
    estimated_requests,
    response_bytes_sum,
    response_bytes_max,
    duration_ms_sum,
    duration_ms_max,
    updated_at
  )
  values (
    v_bucket,
    btrim(p_endpoint),
    upper(btrim(p_method)),
    v_status_class,
    1,
    v_estimated_requests,
    v_response_bytes,
    v_response_bytes,
    v_duration_ms,
    v_duration_ms,
    now()
  )
  on conflict (bucket_minute, endpoint, method, status_class)
  do update set
    observed_requests = uk_aq_raw.endpoint_egress_metrics_minute.observed_requests + 1,
    estimated_requests = uk_aq_raw.endpoint_egress_metrics_minute.estimated_requests + excluded.estimated_requests,
    response_bytes_sum = uk_aq_raw.endpoint_egress_metrics_minute.response_bytes_sum + excluded.response_bytes_sum,
    response_bytes_max = greatest(uk_aq_raw.endpoint_egress_metrics_minute.response_bytes_max, excluded.response_bytes_max),
    duration_ms_sum = uk_aq_raw.endpoint_egress_metrics_minute.duration_ms_sum + excluded.duration_ms_sum,
    duration_ms_max = greatest(uk_aq_raw.endpoint_egress_metrics_minute.duration_ms_max, excluded.duration_ms_max),
    updated_at = now();

  if v_status = 304 or v_status >= 400 then
    insert into uk_aq_raw.endpoint_egress_events (
      occurred_at,
      endpoint,
      method,
      status,
      status_class,
      duration_ms,
      response_bytes,
      sample_rate,
      request_meta
    )
    values (
      coalesce(p_occurred_at, now()),
      btrim(p_endpoint),
      upper(btrim(p_method)),
      v_status,
      v_status_class,
      v_duration_ms,
      v_response_bytes,
      v_sample_rate::numeric(10,6),
      coalesce(p_request_meta, '{}'::jsonb)
    );
  end if;
end;
$$;

create or replace function uk_aq_public.uk_aq_cleanup_endpoint_metrics(
  p_aggregate_retention_days integer default 30,
  p_event_retention_days integer default 7
)
returns table (
  aggregate_rows_deleted bigint,
  event_rows_deleted bigint
)
language plpgsql
security definer
set search_path = uk_aq_raw, uk_aq_public, public, pg_catalog
as $$
declare
  v_agg_days integer := greatest(1, coalesce(p_aggregate_retention_days, 30));
  v_evt_days integer := greatest(1, coalesce(p_event_retention_days, 7));
  v_agg_cutoff timestamptz := now() - make_interval(days => v_agg_days);
  v_evt_cutoff timestamptz := now() - make_interval(days => v_evt_days);
  v_agg_deleted bigint := 0;
  v_evt_deleted bigint := 0;
begin
  delete from uk_aq_raw.endpoint_egress_metrics_minute
  where bucket_minute < v_agg_cutoff;
  get diagnostics v_agg_deleted = row_count;

  delete from uk_aq_raw.endpoint_egress_events
  where occurred_at < v_evt_cutoff;
  get diagnostics v_evt_deleted = row_count;

  return query
  select v_agg_deleted, v_evt_deleted;
end;
$$;

create or replace view uk_aq_public.uk_aq_endpoint_egress_metrics_minute as
select
  bucket_minute,
  endpoint,
  method,
  status_class,
  observed_requests,
  estimated_requests,
  response_bytes_sum,
  response_bytes_max,
  duration_ms_sum,
  duration_ms_max,
  case when observed_requests > 0 then round((response_bytes_sum::numeric / observed_requests), 2) else 0 end as response_bytes_avg,
  case when observed_requests > 0 then round((duration_ms_sum::numeric / observed_requests), 2) else 0 end as duration_ms_avg,
  updated_at
from uk_aq_raw.endpoint_egress_metrics_minute;
alter view if exists uk_aq_public.uk_aq_endpoint_egress_metrics_minute set (security_invoker = true);

revoke all on function uk_aq_public.uk_aq_record_endpoint_metric(
  text,
  text,
  integer,
  integer,
  integer,
  double precision,
  timestamptz,
  jsonb
) from public;
grant execute on function uk_aq_public.uk_aq_record_endpoint_metric(
  text,
  text,
  integer,
  integer,
  integer,
  double precision,
  timestamptz,
  jsonb
) to service_role;

revoke all on function uk_aq_public.uk_aq_cleanup_endpoint_metrics(integer, integer) from public;
grant execute on function uk_aq_public.uk_aq_cleanup_endpoint_metrics(integer, integer) to service_role;

revoke all on uk_aq_public.uk_aq_endpoint_egress_metrics_minute from public;
grant select on uk_aq_public.uk_aq_endpoint_egress_metrics_minute to authenticated;
grant select on uk_aq_public.uk_aq_endpoint_egress_metrics_minute to service_role;
