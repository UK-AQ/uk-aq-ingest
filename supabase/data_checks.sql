-- Data checks for UK AQ Supabase tables.
-- Use this file to sanity-check ingest and geometry coverage.

-- Connectors and station counts per connector.
select
  svc.id,
  svc.connector_code,
  svc.label,
  count(stn.id) as station_count
from connectors svc
left join stations stn on stn.connector_id = svc.id
group by svc.id, svc.connector_code, svc.label
order by station_count desc;

-- Stations: total, with geometry, and missing geometry.
select
  count(*) as stations_total,
  count(*) filter (where geometry is not null) as stations_with_geom,
  count(*) filter (where geometry is null) as stations_missing_geom
from stations;

-- Stations geometry SRIDs (should be 4326).
select distinct ST_SRID(geometry::geometry) as station_srid
from stations
where geometry is not null;

-- Station geometry bounds (lon/lat ranges).
select
  min(ST_X(geometry::geometry)) as min_lon,
  max(ST_X(geometry::geometry)) as max_lon,
  min(ST_Y(geometry::geometry)) as min_lat,
  max(ST_Y(geometry::geometry)) as max_lat
from stations
where geometry is not null;

-- Timeseries missing station links.
select count(*) as timeseries_missing_station
from timeseries
where station_id is null;

-- Recent observation volume (last 24 hours).
select count(*) as observations_last_24h
from observations
where observed_at >= now() - interval '24 hours';

-- Latest observation timestamp.
select max(observed_at) as latest_observed_at
from observations;

-- PCON boundaries by version.
select pcon_version, count(*) as boundary_count
from pcon_boundaries
group by pcon_version
order by pcon_version desc;

-- Station PCON history by version.
select pcon_version, count(*) as history_count
from station_pcon_history
group by pcon_version
order by pcon_version desc;

-- Stations missing PCON history for a version (update the version as needed).
select count(*) as stations_missing_pcon_history
from stations st
left join station_pcon_history sph
  on sph.station_id = st.id
  and sph.pcon_version = '2024'
where st.geometry is not null
  and sph.station_id is null;

-- Top constituencies by station count for a version (update the version as needed).
select
  pcon_code,
  pcon_name,
  count(*) as station_count
from station_pcon_history
where pcon_version = '2024'
group by pcon_code, pcon_name
order by station_count desc
limit 10;
