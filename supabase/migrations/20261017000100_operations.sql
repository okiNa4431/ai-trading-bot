-- Additive Phase 5: no existing account/ledger/quota rows are rewritten.
-- Test-only operation controller. All times and fencing decisions use DB time.
-- Private pre-migration snapshots: source rows remain unchanged. Not a substitute
-- for an off-site database backup, but permits before/after integrity comparison.
create table if not exists bot_private.phase5_before_accounts as table bot_private.accounts;
create table if not exists bot_private.phase5_before_records as table bot_private.records;
create table if not exists bot_private.phase5_before_balances as table bot_private.balances;
create table if not exists bot_private.phase5_before_positions as table bot_private.positions;
alter table bot_private.phase5_before_accounts enable row level security;
alter table bot_private.phase5_before_records enable row level security;
alter table bot_private.phase5_before_balances enable row level security;
alter table bot_private.phase5_before_positions enable row level security;
create table if not exists bot_private.operation_runs (
 account text not null check(account ~ '^test-phase5-[a-z0-9-]+$'),
 run_id text not null, token bigint generated always as identity unique,
 owner text not null, lease_until timestamptz not null,
 state text not null default 'RUNNING' check(state in
 ('RUNNING','SUCCEEDED','FAILED','PARTIAL','NEEDS_REVIEW','SKIPPED')),
 stage integer not null default 0 check(stage between 0 and 8),
 metadata jsonb not null, outcome jsonb,
 started_at timestamptz not null default clock_timestamp(),
 updated_at timestamptz not null default clock_timestamp(),
 primary key(account,run_id)
);
create table if not exists bot_private.operation_controls (
 account text primary key check(account ~ '^test-phase5-[a-z0-9-]+$'),
 stopped boolean not null default false, reason text not null default ''
);
create table if not exists bot_private.operation_events (
 id bigint generated always as identity primary key,
 account text not null, run_id text, at timestamptz not null default clock_timestamp(),
 event jsonb not null
);
-- Small test universes: immutable private JSON generations, not public artifacts/cache.
create table if not exists bot_private.operation_data (
 account text not null, key text not null, generation bigint generated always as identity,
 run_id text not null, payload jsonb not null, at timestamptz not null default clock_timestamp(),
 primary key(account,key,generation), check(pg_column_size(payload)<4000000)
);
alter table bot_private.operation_runs enable row level security;
alter table bot_private.operation_controls enable row level security;
alter table bot_private.operation_events enable row level security;
alter table bot_private.operation_data enable row level security;
revoke all on all tables in schema bot_private from public,anon,authenticated,service_role;

create or replace function bot_private.operation_check(p_account text,p_run text,p_token bigint)
returns void language plpgsql security definer set search_path='' as $$
begin
 perform pg_advisory_xact_lock(490016001);
 if p_account !~ '^test-phase5-[a-z0-9-]+$' or not exists(
 select 1 from bot_private.operation_runs where account=p_account and run_id=p_run
 and token=p_token and state='RUNNING' and lease_until>clock_timestamp())
 or exists(select 1 from bot_private.operation_controls where account=p_account and stopped)
 then raise exception 'operation fenced, stopped, expired or unknown'; end if;
end $$;

create or replace function public.bot_operation_claim(p_account text,p_run text,p_owner text,
 p_metadata jsonb,p_seconds integer default 600) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r bot_private.operation_runs;
begin
 perform pg_advisory_xact_lock(490016001);
 if p_account !~ '^test-phase5-[a-z0-9-]+$' or length(p_run)>180 or length(p_owner)>180
 or p_seconds not between 10 and 900 or jsonb_typeof(p_metadata)<>'object'
 then raise exception 'invalid test operation'; end if;
 if exists(select 1 from bot_private.operation_controls where account=p_account and stopped)
 then raise exception 'account stopped'; end if;
 select * into r from bot_private.operation_runs where account=p_account and run_id=p_run;
 if found then
   if r.metadata is distinct from p_metadata then raise exception 'slot configuration changed'; end if;
   if r.state in ('SUCCEEDED','SKIPPED') then return to_jsonb(r); end if;
   raise exception 'existing operation requires inspection; no automatic replay';
 end if;
 if exists(select 1 from bot_private.operation_runs where account=p_account
 and state in ('RUNNING','NEEDS_REVIEW','PARTIAL','FAILED'))
 then raise exception 'unresolved account operation; inspect before continuing'; end if;
 insert into bot_private.operation_runs(account,run_id,owner,lease_until,metadata)
 values(p_account,p_run,p_owner,clock_timestamp()+make_interval(secs=>p_seconds),p_metadata)
 returning * into r;
 return to_jsonb(r);
end $$;

create or replace function public.bot_operation_step(p_account text,p_run text,p_token bigint,
 p_stage integer,p_state text default 'RUNNING',p_outcome jsonb default '{}'::jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r bot_private.operation_runs;
begin
 perform bot_private.operation_check(p_account,p_run,p_token);
 select * into r from bot_private.operation_runs where account=p_account and run_id=p_run;
 if p_stage<r.stage or p_stage>r.stage+1 or p_stage>8
 or p_state not in ('RUNNING','SUCCEEDED','FAILED','PARTIAL','NEEDS_REVIEW','SKIPPED')
 or (p_state='SUCCEEDED' and p_stage<>8) then raise exception 'invalid operation transition'; end if;
 update bot_private.operation_runs set stage=p_stage,state=p_state,outcome=p_outcome,
 updated_at=clock_timestamp(),lease_until=clock_timestamp()+interval '600 seconds'
 where account=p_account and run_id=p_run returning * into r;
 insert into bot_private.operation_events(account,run_id,event)
 values(p_account,p_run,jsonb_build_object('stage',p_stage,'state',p_state,'outcome',p_outcome));
 return to_jsonb(r);
end $$;

create or replace function public.bot_operation_read(p_account text) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if p_account !~ '^test-phase5-[a-z0-9-]+$' then raise exception 'test scope required'; end if;
 return jsonb_build_object('runs',coalesce((select jsonb_agg(to_jsonb(r) order by token)
 from bot_private.operation_runs r where account=p_account),'[]'::jsonb),
 'stopped',coalesce((select stopped from bot_private.operation_controls where account=p_account),false),
 'data',coalesce((select jsonb_object_agg(key,payload) from (
 select distinct on (key) key,payload from bot_private.operation_data where account=p_account
 order by key,generation desc) d),'{}'::jsonb));
end $$;

-- Reconciliation closes an uncertain attempt, never replays it or refunds quota.
-- A fresh future slot may run only after explicit reviewed closure. No trading mutations here.
create or replace function public.bot_operation_review(p_account text,p_run text,p_reason text,
 p_stop boolean default true) returns void
language plpgsql security definer set search_path='' as $$
begin
 perform pg_advisory_xact_lock(490016001);
 if p_account !~ '^test-phase5-[a-z0-9-]+$' or length(trim(p_reason))<12
 then raise exception 'test scope and review reason required'; end if;
 if not p_stop and exists(select 1 from bot_private.operation_runs where account=p_account and state='RUNNING'
 and lease_until>clock_timestamp()) then raise exception 'active runner must finish or expire'; end if;
 update bot_private.operation_runs set state=case when p_stop then 'NEEDS_REVIEW' else 'SKIPPED' end,updated_at=clock_timestamp()
 where account=p_account and run_id=p_run and state in ('RUNNING','NEEDS_REVIEW','FAILED','PARTIAL');
 insert into bot_private.operation_controls values(p_account,p_stop,p_reason)
 on conflict(account) do update set stopped=excluded.stopped,reason=excluded.reason;
 insert into bot_private.operation_events(account,run_id,event)
 values(p_account,p_run,jsonb_build_object('review_reason',p_reason,'stopped',p_stop));
end $$;

-- Mandatory guards also cover legacy RPCs and account deletion: no bypass through old CLI.
create or replace function bot_private.operation_guard() returns trigger
language plpgsql security definer set search_path='' as $$
declare a text; v jsonb;
begin
 v:=case when tg_op='DELETE' then to_jsonb(old) else to_jsonb(new) end;
 a:=case when tg_table_name='accounts' then v->>'id' else v->>'account_id' end;
 if a like 'test-phase5-%' then
   if tg_op='DELETE' then raise exception 'operation accounts retain audit history'; end if;
   perform bot_private.operation_check(a,current_setting('bot.operation_run',true),
     nullif(current_setting('bot.operation_token',true),'')::bigint);
 elsif a='paper-live' then
   if tg_table_name='records' and v->>'kind' in ('orders','executions','fx_exchanges','cash_ledger')
   and not (v->>'kind'='cash_ledger' and v->'payload'->>'kind'='INITIAL'
     and not exists(select 1 from bot_private.accounts where id=a and value is not null))
   then raise exception 'paper-live financial mutations disabled'; end if;
   if tg_table_name='accounts' and tg_op='UPDATE'
   and to_jsonb(old)->'value' <> 'null'::jsonb and (
    to_jsonb(old)->'value'->'balances' is distinct from v->'value'->'balances' or
    to_jsonb(old)->'value'->'positions' is distinct from v->'value'->'positions' or
    to_jsonb(old)->'value'->'initial_jpy' is distinct from v->'value'->'initial_jpy')
   then raise exception 'paper-live financial mutations disabled'; end if;
 end if;
 if tg_op='DELETE' then return old; end if; return new;
end $$;
drop trigger if exists operation_guard on bot_private.accounts;
create trigger operation_guard before insert or update or delete on bot_private.accounts
for each row execute function bot_private.operation_guard();
drop trigger if exists operation_guard on bot_private.records;
create trigger operation_guard before insert or update or delete on bot_private.records
for each row execute function bot_private.operation_guard();

create or replace function public.bot_operation_commit(p_account text,p_run text,p_token bigint,
 p_test boolean,p_version bigint,p_value jsonb,p_writes jsonb) returns bigint
language plpgsql security definer set search_path='' as $$
declare v bigint;
begin
 perform bot_private.operation_check(p_account,p_run,p_token);
 perform set_config('bot.operation_run',p_run,true);
 perform set_config('bot.operation_token',p_token::text,true);
 v:=public.bot_commit(p_account,p_test,p_version,p_value,p_writes);
 perform bot_private.operation_check(p_account,p_run,p_token);
 return v;
end $$;
create or replace function public.bot_operation_reserve(p_account text,p_run text,p_token bigint,
 p_decision text,p_request text,p_model text,p_day_limit integer,p_month_limit integer,p_run_limit integer)
returns jsonb language plpgsql security definer set search_path='' as $$
declare v jsonb;
begin
 perform bot_private.operation_check(p_account,p_run,p_token);
 perform set_config('bot.operation_run',p_run,true);
 perform set_config('bot.operation_token',p_token::text,true);
 v:=public.bot_reserve_api(p_account,p_decision,p_request,p_model,p_day_limit,p_month_limit,p_run_limit);
 perform bot_private.operation_check(p_account,p_run,p_token);
 return v;
end $$;
create or replace function public.bot_operation_data(p_account text,p_run text,p_token bigint,
 p_key text,p_payload jsonb) returns void
language plpgsql security definer set search_path='' as $$
begin
 perform bot_private.operation_check(p_account,p_run,p_token);
 if length(p_key)>160 or pg_column_size(p_payload)>4000000 then raise exception 'data limit'; end if;
 insert into bot_private.operation_data(account,key,run_id,payload) values(p_account,p_key,p_run,p_payload);
end $$;

revoke all on all functions in schema bot_private from public,anon,authenticated,service_role;
revoke all on function public.bot_operation_claim(text,text,text,jsonb,integer),
 public.bot_operation_step(text,text,bigint,integer,text,jsonb), public.bot_operation_read(text),
 public.bot_operation_review(text,text,text,boolean),
 public.bot_operation_commit(text,text,bigint,boolean,bigint,jsonb,jsonb),
 public.bot_operation_reserve(text,text,bigint,text,text,text,integer,integer,integer),
 public.bot_operation_data(text,text,bigint,text,jsonb) from public,anon,authenticated;
grant execute on function public.bot_operation_claim(text,text,text,jsonb,integer),
 public.bot_operation_step(text,text,bigint,integer,text,jsonb), public.bot_operation_read(text),
 public.bot_operation_review(text,text,text,boolean),
 public.bot_operation_commit(text,text,bigint,boolean,bigint,jsonb,jsonb),
 public.bot_operation_reserve(text,text,bigint,text,text,text,integer,integer,integer),
 public.bot_operation_data(text,text,bigint,text,jsonb) to service_role;
