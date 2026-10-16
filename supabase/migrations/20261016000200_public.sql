-- A dedicated read model, never an owner-rights view over internal tables.
create table if not exists public.bot_dashboard (
  id text primary key check (id='paper-live'),
  document jsonb not null,
  published_at timestamptz not null default now()
);
alter table public.bot_dashboard enable row level security;
revoke all on public.bot_dashboard from public,anon,authenticated,service_role;
grant select on public.bot_dashboard to anon,authenticated;
drop policy if exists public_dashboard_read on public.bot_dashboard;
create policy public_dashboard_read on public.bot_dashboard for select to anon,authenticated
  using (id='paper-live');

create or replace function public.bot_publish(p_account text,p_version bigint,p_document jsonb)
returns void language plpgsql security definer set search_path = '' as $$
begin
  perform pg_advisory_xact_lock(490016001);
  if not exists(select 1 from bot_private.accounts where id=p_account and not is_test
    and id='paper-live' and version=p_version) then
    raise exception using errcode='40001',message='publication scope or version mismatch';
  end if;
  if exists(select 1 from jsonb_object_keys(p_document) k
    where k not in ('schema_version','state','updated_at','started_at','summary','series',
      'holdings','trades','decisions','usage','warnings')) or
      (p_document->>'schema_version')::int<>1 then raise exception 'invalid public contract'; end if;
  insert into public.bot_dashboard(id,document) values(p_account,p_document)
    on conflict(id) do update set document=excluded.document,published_at=now();
end $$;
revoke all on function public.bot_publish(text,bigint,jsonb) from public,anon,authenticated;
grant execute on function public.bot_publish(text,bigint,jsonb) to service_role;

notify pgrst, 'reload schema';
