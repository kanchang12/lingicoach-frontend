-- Run once in Supabase SQL Editor

create table if not exists user_stats (
  user_id uuid primary key references auth.users on delete cascade,
  native_language text,
  streak int default 0,
  last_activity date,
  is_premium boolean default false,
  premium_expires_at timestamptz,
  consent_given boolean default false
);

create table if not exists user_progress (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users on delete cascade,
  scenario_id text not null,
  done boolean default true,
  updated_at timestamptz default now(),
  unique(user_id, scenario_id)
);

alter table user_stats enable row level security;
alter table user_progress enable row level security;

create policy "own stats" on user_stats
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create policy "own progress" on user_progress
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
