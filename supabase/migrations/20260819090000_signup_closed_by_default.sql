-- ===========================================================================
-- Closed unless someone says otherwise
--
-- The column defaulted to open so that the first account could be created at
-- all. But the gate already handles that case separately — signup is allowed
-- whenever there are no users, because otherwise nobody could ever make the
-- first one — so the default was doing nothing except leaving a window open.
--
-- It mattered when setup was abandoned halfway. Create the first account, close
-- the browser before finishing the wizard, and the instance sat there accepting
-- registrations from anyone who found it.
-- ===========================================================================

alter table public.instance_settings alter column signup_open set default false;

-- Only where nobody has been through setup. An instance whose owner chose to
-- leave signup open is not one to quietly close behind them.
update public.instance_settings
   set signup_open = false
 where setup_completed_at is null;
