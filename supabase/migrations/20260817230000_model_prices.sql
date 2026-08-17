-- ===========================================================================
-- What a model costs, when we do not already know
--
-- Moonphase ships rates for models whose pricing is public and stable, and
-- reports tokens without a cost for anything else. Inventing a rate would
-- produce a confident number that happens to be wrong, which is a worse answer
-- about someone's bill than no answer.
--
-- But "no answer" is not good enough either when the model you actually use is
-- the one we have no rate for. So rates are editable, per organization, and an
-- entry here wins over the built-in table.
-- ===========================================================================

create table public.model_prices (
  org_id       uuid not null references public.organizations (id) on delete cascade,
  -- Matched as a prefix, longest wins, so `claude-sonnet-5` covers every dated
  -- release of it without an entry apiece.
  model        text not null,
  input_per_m  numeric(12, 4) not null check (input_per_m >= 0),
  output_per_m numeric(12, 4) not null check (output_per_m >= 0),
  updated_at   timestamptz not null default now(),
  primary key (org_id, model)
);

alter table public.model_prices enable row level security;

create policy model_prices_select on public.model_prices
  for select to authenticated
  using (public.is_org_member(org_id));

create policy model_prices_write on public.model_prices
  for all to authenticated
  using (public.has_org_role(org_id, 'owner', 'admin', 'member'))
  with check (public.has_org_role(org_id, 'owner', 'admin', 'member'));

grant select, insert, update, delete on public.model_prices to authenticated;
grant all on public.model_prices to service_role;
