-- Additive only. No existing portfolio, API history or public projection is modified.
create schema if not exists bot_research_private;
revoke all on schema bot_research_private from public, anon, authenticated;
create table if not exists bot_research_private.accounts (
 account_id text primary key check (account_id ~ '^test-integration-[a-z0-9-]{1,40}$'),
 version bigint not null default 0, state jsonb not null default '{}'::jsonb
);
create table if not exists bot_research_private.observations (
 account_id text not null references bot_research_private.accounts(account_id),
 observation_id text not null, payload jsonb not null,
 primary key(account_id, observation_id)
);
create table if not exists bot_research_private.runs (
 run_id text primary key, account_id text not null references bot_research_private.accounts(account_id),
 request_hash text not null, result jsonb not null
);
alter table bot_research_private.accounts enable row level security;
alter table bot_research_private.accounts force row level security;
alter table bot_research_private.observations enable row level security;
alter table bot_research_private.observations force row level security;
alter table bot_research_private.runs enable row level security;
alter table bot_research_private.runs force row level security;
revoke all on all tables in schema bot_research_private from public,anon,authenticated,service_role;

create or replace function public.bot_research_read(p_account text)
returns jsonb language plpgsql security definer set search_path = pg_catalog as $$
declare v bigint; s jsonb;
begin
 if p_account !~ '^test-integration-[a-z0-9-]{1,40}$' then raise exception 'research scope denied'; end if;
 select version,state into v,s from bot_research_private.accounts where account_id=p_account;
 return jsonb_build_object('version',coalesce(v,0),'state',coalesce(s,'{"account":null,"records":[]}'::jsonb));
end $$;
create or replace function public.bot_research_run(p_account text,p_run text)
returns jsonb language plpgsql security definer set search_path = pg_catalog as $$
begin
 if p_account !~ '^test-integration-[a-z0-9-]{1,40}$' then raise exception 'research scope denied'; end if;
 return (select result from bot_research_private.runs where account_id=p_account and run_id=p_run);
end $$;
create or replace function public.bot_research_commit(
 p_account text,p_run text,p_version bigint,p_request_hash text,
 p_bars jsonb,p_state jsonb,p_result jsonb
) returns jsonb language plpgsql security definer set search_path = pg_catalog as $$
declare v bigint; r record; b jsonb; old jsonb;
begin
 if p_account is null or p_run is null or p_version is null or p_request_hash is null
 or p_bars is null or p_state is null or p_result is null
 or p_account !~ '^test-integration-[a-z0-9-]{1,40}$' or length(p_run) not between 1 and 128
 or p_version < 0 or p_request_hash !~ '^[0-9a-f]{64}$'
 or jsonb_typeof(p_state) <> 'object' or jsonb_typeof(p_result) <> 'object'
 or jsonb_typeof(p_bars) <> 'array' then raise exception 'invalid research transaction'; end if;
 if jsonb_array_length(p_bars)>500000 or octet_length(p_bars::text)>50000000
 then raise exception 'research batch budget exceeded'; end if;
 perform pg_advisory_xact_lock(hashtextextended('research:'||p_account,0));
 select * into r from bot_research_private.runs where run_id=p_run;
 if found then
  if r.account_id<>p_account or r.request_hash<>p_request_hash then
   raise exception using errcode='40001',message='research run identity conflict';
  end if;
  return r.result;
 end if;
 insert into bot_research_private.accounts(account_id) values(p_account) on conflict do nothing;
 select version into v from bot_research_private.accounts where account_id=p_account for update;
 if v<>p_version then raise exception using errcode='40001',message='research stale writer'; end if;
 for b in select value from jsonb_array_elements(p_bars) loop
  if (b->>'id') !~ '^[0-9a-f]{64}$' or jsonb_typeof(b->'payload')<>'object'
  then raise exception 'invalid research observation'; end if;
  select payload into old from bot_research_private.observations
   where account_id=p_account and observation_id=b->>'id';
  if found and (old-'fetched_at')<>((b->'payload')-'fetched_at') then
   raise exception 'research observation identity conflict';
  end if;
  insert into bot_research_private.observations values(p_account,b->>'id',b->'payload')
   on conflict do nothing;
 end loop;
 update bot_research_private.accounts set version=v+1,state=p_state where account_id=p_account;
 insert into bot_research_private.runs values(p_run,p_account,p_request_hash,p_result);
 return p_result;
end $$;
revoke all on function public.bot_research_read(text) from public,anon,authenticated;
revoke all on function public.bot_research_run(text,text) from public,anon,authenticated;
revoke all on function public.bot_research_commit(text,text,bigint,text,jsonb,jsonb,jsonb)
 from public,anon,authenticated;
grant execute on function public.bot_research_read(text) to service_role;
grant execute on function public.bot_research_run(text,text) to service_role;
grant execute on function public.bot_research_commit(text,text,bigint,text,jsonb,jsonb,jsonb)
 to service_role;
