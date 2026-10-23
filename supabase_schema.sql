-- Sketch2App — Fase 2 (Item A: Hub de Projetos no Supabase)
-- Execute este script no SQL Editor do Supabase para criar as tabelas com RLS ativo.

create table if not exists public.projects (
  sid text primary key,
  title text not null,
  prompt text not null default '',
  image_path text,
  current_version integer not null default 1,
  created_at timestamptz not null default now()
);

create table if not exists public.versions (
  project_sid text not null references public.projects(sid) on delete cascade,
  n integer not null,
  html text not null,
  summary text not null,
  model text,
  tokens_in integer,
  tokens_out integer,
  latency_ms integer,
  created_at timestamptz not null default now(),
  primary key (project_sid, n)
);

create table if not exists public.messages (
  project_sid text not null references public.projects(sid) on delete cascade,
  seq integer not null,
  role text not null check (role in ('user', 'assistant')),
  text text not null,
  created_at timestamptz not null default now(),
  primary key (project_sid, seq)
);

-- RLS ativo em todas as tabelas (acesso apenas via SUPABASE_SERVICE_KEY no backend)
alter table public.projects enable row level security;
alter table public.versions enable row level security;
alter table public.messages enable row level security;
