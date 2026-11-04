-- 가입 무료 크레딧을 같은 IP에서 하루(최근 24시간) 3명까지만 준다(섹션 8 공격 시험에서 게스트 재가입이 뚫려서).
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행한다. staging 과 production 프로젝트에 각각 한 번씩.
--
-- - IP 원문은 저장하지 않는다. 서버가 비밀값과 섞어 만든 해시(ip_hash)만 가입 지급 줄에 적는다.
-- - 같은 IP의 가입 요청은 잠금으로 한 줄로 세워, 동시에 여러 게스트를 만들어도 한도를 넘지 못하게 한다.
-- - 한도를 넘은 게스트는 아무 줄도 쌓지 않는다(0에서 시작해 충전할 수 있다).

alter table public.credit_ledger add column if not exists ip_hash text;
create index if not exists credit_ledger_signup_ip_idx on public.credit_ledger (ip_hash, created_at)
  where reason = 'signup';

-- 결과: 'granted'(이번에 지급) / 'exists'(이미 받음) / 'limited'(이 IP의 하루 한도를 넘음)
create or replace function public.grant_signup(p_user uuid, p_amount integer, p_ip_hash text, p_daily_limit integer)
returns text
language plpgsql
set search_path = public
as $$
declare
  given integer;
begin
  perform pg_advisory_xact_lock(hashtext('signup-ip:' || coalesce(p_ip_hash, '')));
  if exists (select 1 from public.credit_ledger where ref = 'signup:' || p_user::text) then
    return 'exists';
  end if;
  if p_ip_hash is not null then
    select count(*) into given from public.credit_ledger
      where reason = 'signup' and ip_hash = p_ip_hash and created_at > now() - interval '24 hours';
    if given >= p_daily_limit then
      return 'limited';
    end if;
  end if;
  insert into public.credit_ledger (user_id, delta, reason, ref, ip_hash)
    values (p_user, p_amount, 'signup', 'signup:' || p_user::text, p_ip_hash);
  return 'granted';
end;
$$;

revoke all on function public.grant_signup(uuid, integer, text, integer) from public, anon, authenticated;
grant execute on function public.grant_signup(uuid, integer, text, integer) to service_role;
