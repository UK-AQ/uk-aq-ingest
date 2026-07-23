create table if not exists uk_aq_raw.blondon_nodes_station_checkpoints (
  station_id bigint primary key
    references uk_aq_core.stations(id) on delete cascade,
  next_due_at timestamptz null,
  last_observed_at timestamptz null,
  ingest_lag_samples integer[] not null default '{}'::integer[],
  last_polled_at timestamptz null,
  last_error text null,
  species_last_observed_at jsonb not null default '{}'::jsonb,
  species_last_error jsonb not null default '{}'::jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists blondon_nodes_station_checkpoints_next_due_at_idx
  on uk_aq_raw.blondon_nodes_station_checkpoints (next_due_at);
create index if not exists blondon_nodes_station_checkpoints_last_polled_at_idx
  on uk_aq_raw.blondon_nodes_station_checkpoints (last_polled_at);

alter table uk_aq_raw.blondon_nodes_station_checkpoints enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'uk_aq_raw'
      and tablename = 'blondon_nodes_station_checkpoints'
      and policyname = 'blondon_nodes_station_checkpoints_select_service_role'
  ) then
    create policy blondon_nodes_station_checkpoints_select_service_role
      on uk_aq_raw.blondon_nodes_station_checkpoints
      for select using ((select auth.role()) = 'service_role');
  end if;
  if not exists (
    select 1 from pg_policies
    where schemaname = 'uk_aq_raw'
      and tablename = 'blondon_nodes_station_checkpoints'
      and policyname = 'blondon_nodes_station_checkpoints_write_service_role'
  ) then
    create policy blondon_nodes_station_checkpoints_write_service_role
      on uk_aq_raw.blondon_nodes_station_checkpoints
      for all using ((select auth.role()) = 'service_role')
      with check ((select auth.role()) = 'service_role');
  end if;
end $$;

grant all on uk_aq_raw.blondon_nodes_station_checkpoints to service_role;
