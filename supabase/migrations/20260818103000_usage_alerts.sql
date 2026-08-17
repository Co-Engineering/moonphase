-- ===========================================================================
-- Tell me before I run out, not after
--
-- A limit you only discover by hitting it is the worst kind: the session stops
-- mid-task, on a machine you are not sitting at. Moonphase already knows how
-- full the window is and already has a push channel, so the alert is a
-- threshold and a note of which window it has already fired for.
--
-- The window anchor is what stops it repeating. Firing per window rather than
-- per check means a threshold crossed at 60% stays crossed without sending a
-- notification every two minutes for the next four hours.
-- ===========================================================================

alter table public.usage_limits
  add column alert_percent int check (alert_percent between 1 and 100),
  -- The anchor of the window an alert has already been sent for.
  add column alerted_window timestamptz,
  add column alerted_week   timestamptz;
