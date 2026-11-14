-- Electron tables. Paste into the Supabase SQL editor and run.
-- Mirrors shared/schema.py. Change both together, and tell the team.

create extension if not exists "pgcrypto";

create table if not exists techs (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  phone       text not null unique,
  skills      text[] not null default '{}',
  lat         double precision not null,
  lng         double precision not null,
  rating      real not null default 4.5,
  calendar_id text
);

create table if not exists jobs (
  id               uuid primary key default gen_random_uuid(),
  customer_phone   text not null,
  photo_url        text,
  raw_text         text not null default '',
  trade            text,
  issue            text,
  urgency          text check (urgency in ('high','medium','low')),
  parts            jsonb not null default '[]',   -- [{name, confidence, url?, price?}]
  location         jsonb,                          -- {lat, lng}
  status           text not null default 'new'
                   check (status in ('new','triaged','offered','accepted','booked','failed')),
  assigned_tech_id uuid references techs(id),
  quote            jsonb,                          -- {low, high}
  created_at       timestamptz not null default now()
);

create table if not exists offers (
  id       uuid primary key default gen_random_uuid(),
  job_id   uuid not null references jobs(id) on delete cascade,
  tech_id  uuid not null references techs(id),
  sent_at  timestamptz not null default now(),
  response text check (response in ('yes','no','timeout'))  -- null = waiting
);

create table if not exists events (
  id         uuid primary key default gen_random_uuid(),
  job_id     uuid,  -- no FK on purpose: B/C can log while testing without a job row
  agent      text not null,
  message    text not null,
  created_at timestamptz not null default now()
);

-- Owned by A. Photon can only reply into a conversation the person started,
-- so we save each phone's Photon space + line user from their inbound message.
create table if not exists contacts (
  id           text primary key,   -- phone, E.164
  space_id     text not null,      -- pho_spc_...
  line_user_id text not null,      -- pho_usr_... (receivedBy.id, used as senderId)
  updated_at   timestamptz not null default now()
);

create index if not exists events_job_idx on events(job_id, created_at);
create index if not exists offers_job_idx on offers(job_id);

-- Hackathon: the backend uses the service-role key, which bypasses RLS.
-- The dashboard reads through FastAPI (/api/...), so no anon policies are needed.
