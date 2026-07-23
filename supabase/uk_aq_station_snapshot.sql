-- Station snapshot RPC for local/dev inspection dashboards.
-- Returns raw rows for a selected station, related timeseries/checkpoints,
-- and observations for a selected/default timeseries in a requested window.

create index if not exists stations_station_ref_idx
  on uk_aq_core.stations(station_ref);

create index if not exists observations_timeseries_observed_at_desc_idx
  on uk_aq_core.observations(timeseries_id, observed_at desc);

drop function if exists uk_aq_public.uk_aq_station_snapshot(
  bigint,
  text,
  bigint,
  text,
  integer
);

drop function if exists uk_aq_public.uk_aq_station_snapshot(
  bigint,
  text,
  integer,
  text,
  integer
);

create or replace function uk_aq_public.uk_aq_station_snapshot(
  p_station_id bigint default null,
  p_station_ref text default null,
  p_timeseries_id integer default null,
  p_window text default '6h',
  p_obs_limit integer default 100
)
returns jsonb
language plpgsql
security definer
set search_path = uk_aq_core, uk_aq_raw, uk_aq_public, public, pg_catalog
as $$
declare
  v_station_id bigint;
  v_station_ref text;
  v_station_row jsonb;
  v_timeseries_rows jsonb := '[]'::jsonb;
  v_station_checkpoint_rows jsonb := '[]'::jsonb;
  v_timeseries_checkpoint_rows jsonb := '[]'::jsonb;
  v_observations jsonb := '[]'::jsonb;
  v_selected_timeseries_id integer;
  v_timeseries_ids integer[] := '{}'::integer[];
  v_window text := lower(coalesce(nullif(trim(p_window), ''), '6h'));
  v_obs_limit integer := case when p_obs_limit = 1000 then 1000 else 100 end;
  v_now timestamptz := now();
  v_window_start timestamptz;
  v_default_timeseries_rule text := 'lowest_timeseries_id_for_station';
begin
  if v_window not in ('6h', '24h', '7d', '21d', '31d', '90d') then
    v_window := '6h';
  end if;

  if v_window = '24h' then
    v_window_start := v_now - interval '24 hours';
  elsif v_window = '7d' then
    v_window_start := v_now - interval '7 days';
  elsif v_window = '21d' then
    v_window_start := v_now - interval '21 days';
  elsif v_window = '31d' then
    v_window_start := v_now - interval '31 days';
  elsif v_window = '90d' then
    v_window_start := v_now - interval '90 days';
  else
    v_window_start := v_now - interval '6 hours';
  end if;

  if p_station_id is not null then
    select s.id, s.station_ref, to_jsonb(s)
    into v_station_id, v_station_ref, v_station_row
    from uk_aq_core.stations s
    where s.id = p_station_id
    limit 1;
  elsif p_station_ref is not null and btrim(p_station_ref) <> '' then
    select s.id, s.station_ref, to_jsonb(s)
    into v_station_id, v_station_ref, v_station_row
    from uk_aq_core.stations s
    where s.station_ref = p_station_ref
    order by s.id asc
    limit 1;
  end if;

  if v_station_id is null then
    return jsonb_build_object(
      'station', null,
      'timeseries', '[]'::jsonb,
      'stations_checkpoints', '[]'::jsonb,
      'timeseries_checkpoints', '[]'::jsonb,
      'selected_timeseries_id', null,
      'observations', '[]'::jsonb,
      'meta', jsonb_build_object(
        'window', v_window,
        'window_start', v_window_start,
        'window_end', v_now,
        'obs_limit', v_obs_limit,
        'default_timeseries_rule', v_default_timeseries_rule,
        'station_resolution', 'not_found'
      )
    );
  end if;

  select coalesce(jsonb_agg(to_jsonb(t) order by t.id), '[]'::jsonb)
  into v_timeseries_rows
  from uk_aq_core.timeseries t
  where t.station_id = v_station_id;

  select coalesce(array_agg(t.id order by t.id), '{}'::integer[])
  into v_timeseries_ids
  from uk_aq_core.timeseries t
  where t.station_id = v_station_id;

  select coalesce(jsonb_agg(to_jsonb(sc) order by sc.station_id), '[]'::jsonb)
  into v_station_checkpoint_rows
  from uk_aq_raw.openaq_station_checkpoints sc
  where sc.station_id = v_station_id;

  if coalesce(array_length(v_timeseries_ids, 1), 0) > 0 then
    select coalesce(jsonb_agg(to_jsonb(tc) order by tc.station_id, tc.timeseries_id), '[]'::jsonb)
    into v_timeseries_checkpoint_rows
    from uk_aq_raw.openaq_timeseries_checkpoints tc
    where tc.timeseries_id = any(v_timeseries_ids);
  end if;

  if p_timeseries_id is not null then
    select t.id
    into v_selected_timeseries_id
    from uk_aq_core.timeseries t
    where t.id = p_timeseries_id
      and t.station_id = v_station_id
    limit 1;
  end if;

  if v_selected_timeseries_id is null then
    select t.id
    into v_selected_timeseries_id
    from uk_aq_core.timeseries t
    where t.station_id = v_station_id
    order by t.id asc
    limit 1;
  end if;

  if v_selected_timeseries_id is not null then
    select coalesce(jsonb_agg(to_jsonb(obs) order by obs.observed_at desc), '[]'::jsonb)
    into v_observations
    from (
      select o.*
      from uk_aq_core.observations o
      where o.timeseries_id = v_selected_timeseries_id
        and o.observed_at >= v_window_start
        and o.observed_at <= v_now
      order by o.observed_at desc
      limit v_obs_limit
    ) obs;
  end if;

  return jsonb_build_object(
    'station', v_station_row,
    'timeseries', v_timeseries_rows,
    'stations_checkpoints', v_station_checkpoint_rows,
    'timeseries_checkpoints', v_timeseries_checkpoint_rows,
    'selected_timeseries_id', v_selected_timeseries_id,
    'observations', v_observations,
    'meta', jsonb_build_object(
      'window', v_window,
      'window_start', v_window_start,
      'window_end', v_now,
      'obs_limit', v_obs_limit,
      'default_timeseries_rule', v_default_timeseries_rule,
      'station_resolution', 'resolved',
      'resolved_station_id', v_station_id,
      'resolved_station_ref', v_station_ref
    )
  );
end;
$$;

revoke all on function uk_aq_public.uk_aq_station_snapshot(bigint, text, integer, text, integer) from public;
grant execute on function uk_aq_public.uk_aq_station_snapshot(bigint, text, integer, text, integer) to authenticated;
grant execute on function uk_aq_public.uk_aq_station_snapshot(bigint, text, integer, text, integer) to service_role;
