-- 결제 주문. 결제를 시작하기 전에 서버가 주문을 "대기"로 저장해 두고,
-- 결제 승인 때 성공 주소(successUrl)로 온 금액을 이 표의 금액과 비교한다.
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행한다. staging 과 production 프로젝트에 각각 한 번씩.
--
-- - order_id 는 추측할 수 없는 무작위 값(ord_ + 32자리)이고 유일하다. 토스페이먼츠 규칙: 6~64자, 영문·숫자·-·_·=
-- - 금액(amount)과 크레딧 수(credits)는 화면이 보낸 값이 아니라 서버의 상품표에서 찾은 값이다.
-- - 상태: pending(대기) → done(완료) 또는 failed(실패). 대기에서만 바뀐다.
-- - 크레딧은 이 표가 아니라 credit_ledger 에 order:<order_id> 참조로 쌓는다(참조가 유일해서 두 번 쌓이지 않는다).
-- - RLS를 켜고 정책은 만들지 않는다. 서버(secret key)만 읽고 쓴다.

create table if not exists public.orders (
  order_id text primary key check (order_id ~ '^[A-Za-z0-9_=-]{6,64}$'),
  user_id uuid not null references auth.users (id) on delete cascade,
  product_id text not null,
  amount integer not null check (amount > 0),
  credits integer not null check (credits > 0),
  status text not null default 'pending' check (status in ('pending', 'done', 'failed')),
  payment_key text,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists orders_user_id_idx on public.orders (user_id, created_at desc);
alter table public.orders enable row level security;
