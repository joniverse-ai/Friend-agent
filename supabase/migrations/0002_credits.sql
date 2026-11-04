-- 크레딧 원장(ledger). 잔액 칸을 덮어쓰지 않고, 늘고 준 기록을 한 줄씩 쌓는다. 잔액은 delta 의 합이다.
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행한다. staging 과 production 프로젝트에 각각 한 번씩.
--
-- - ref 는 유일하다. 같은 가입, 같은 메시지, 같은 결제가 두 번 기록되지 않는다.
--   signup:<user_id> 가입 무료 크레딧 / msg:<무작위> 메시지 차감 / refund:<같은 무작위> 실패한 메시지 되돌림
--   order:<주문 번호> 결제로 산 크레딧(섹션 6)
-- - 잘못 들어간 줄은 지우지 않고 되돌리는 줄을 새로 쌓는다.
-- - RLS를 켜고 정책은 만들지 않는다. 브라우저는 이 표를 읽거나 쓸 수 없고 서버(secret key)만 쓴다.
-- - 내 데이터 전체 삭제(/api/data)는 이 표를 지우지 않는다. 지우면 무료 크레딧을 다시 받을 수 있다.

create table if not exists public.credit_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  delta integer not null check (delta <> 0),
  reason text not null,                       -- signup, message, refund, purchase
  ref text not null unique,
  created_at timestamptz not null default now()
);
create index if not exists credit_ledger_user_id_idx on public.credit_ledger (user_id, id);
alter table public.credit_ledger enable row level security;

-- 잔액
create or replace function public.credit_balance(p_user uuid)
returns integer
language sql
stable
set search_path = public
as $$
  select coalesce(sum(delta), 0)::integer from public.credit_ledger where user_id = p_user;
$$;

-- 차감. 같은 사용자의 요청은 잠금으로 한 줄로 세워, 잔액 확인과 - 줄 기록을 한 번에 한다.
-- 그래서 잔액 1에서 요청 열 개가 동시에 와도 하나만 통과한다.
-- 잔액이 모자라면 아무것도 쓰지 않고 null 을 돌려준다. 성공하면 차감 뒤 잔액을 돌려준다.
create or replace function public.spend_credit(p_user uuid, p_ref text, p_amount integer default 1)
returns integer
language plpgsql
set search_path = public
as $$
declare
  balance integer;
begin
  perform pg_advisory_xact_lock(hashtext('credit:' || p_user::text));
  select coalesce(sum(delta), 0) into balance from public.credit_ledger where user_id = p_user;
  if balance < p_amount then
    return null;
  end if;
  insert into public.credit_ledger (user_id, delta, reason, ref) values (p_user, -p_amount, 'message', p_ref);
  return balance - p_amount;
end;
$$;

-- 두 함수는 서버(service_role, secret key)만 부를 수 있다. 브라우저 역할(anon, authenticated)은 못 부른다.
revoke all on function public.credit_balance(uuid) from public, anon, authenticated;
revoke all on function public.spend_credit(uuid, text, integer) from public, anon, authenticated;
grant execute on function public.credit_balance(uuid) to service_role;
grant execute on function public.spend_credit(uuid, text, integer) to service_role;
