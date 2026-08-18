import { useState } from 'react'
import type { AuthMethods } from '../lib/api'

/**
 * How people get in.
 *
 * Four ways, and each of the three that need credentials is useless without
 * them — an enabled provider with no client ID renders a button that fails
 * somewhere the person has never heard of. So the form shows what each one
 * still needs, and the server refuses to call a method usable until it is.
 *
 * Secrets are write-only. A form cannot show one back, so an empty field means
 * "leave it alone" rather than "erase it", and the placeholder says so.
 */

export interface Draft {
  password_enabled: boolean
  magic_link_enabled: boolean
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_sender: string
  smtp_password: string
  google_enabled: boolean
  google_client_id: string
  google_client_secret: string
  microsoft_enabled: boolean
  microsoft_client_id: string
  microsoft_client_secret: string
  microsoft_tenant: string
}

export function draftFrom(methods: AuthMethods | null): Draft {
  return {
    password_enabled: methods?.password_enabled ?? true,
    magic_link_enabled: methods?.magic_link_enabled ?? false,
    smtp_host: methods?.smtp_host ?? '',
    smtp_port: methods?.smtp_port ?? 587,
    smtp_user: methods?.smtp_user ?? '',
    smtp_sender: methods?.smtp_sender ?? '',
    smtp_password: '',
    google_enabled: methods?.google_enabled ?? false,
    google_client_id: methods?.google_client_id ?? '',
    google_client_secret: '',
    microsoft_enabled: methods?.microsoft_enabled ?? false,
    microsoft_client_id: methods?.microsoft_client_id ?? '',
    microsoft_client_secret: '',
    microsoft_tenant: methods?.microsoft_tenant ?? 'common',
  }
}

interface Props {
  draft: Draft
  onChange: (next: Draft) => void
  /** Shown so it can be pasted into the provider's console. */
  redirectUri: string
  /** True while there is no saved address, which OAuth cannot work without. */
  addressMissing?: boolean
}

export function SignInMethods({ draft, onChange, redirectUri, addressMissing }: Props) {
  const [showSmtp, setShowSmtp] = useState(false)
  const set = (patch: Partial<Draft>) => onChange({ ...draft, ...patch })

  return (
    <div className="methods">
      <label className="check">
        <input
          type="checkbox"
          checked={draft.password_enabled}
          onChange={(e) => set({ password_enabled: e.target.checked })}
        />
        <span>Email and password</span>
      </label>
      <p className="hint">Needs nothing configured. Leave it on until another way in works.</p>

      <label className="check">
        <input
          type="checkbox"
          checked={draft.magic_link_enabled}
          onChange={(e) => set({ magic_link_enabled: e.target.checked, })}
        />
        <span>Magic link by email</span>
      </label>
      {draft.magic_link_enabled && (
        <div className="method-detail">
          <p className="hint">
            Needs a mail server to send from — there is no way to email a link without
            one.
          </p>
          <div className="pair-row">
            <label>
              <span>SMTP host</span>
              <input
                value={draft.smtp_host}
                onChange={(e) => set({ smtp_host: e.target.value })}
                placeholder="smtp.example.com"
              />
            </label>
            <label>
              <span>Port</span>
              <input
                type="number"
                value={draft.smtp_port}
                onChange={(e) => set({ smtp_port: Number(e.target.value) })}
              />
            </label>
          </div>
          <label>
            <span>Send from</span>
            <input
              value={draft.smtp_sender}
              onChange={(e) => set({ smtp_sender: e.target.value })}
              placeholder="moonphase@example.com"
            />
          </label>
          <div className="pair-row">
            <label>
              <span>Username</span>
              <input
                value={draft.smtp_user}
                onChange={(e) => set({ smtp_user: e.target.value })}
              />
            </label>
            <label>
              <span>Password</span>
              <input
                type={showSmtp ? 'text' : 'password'}
                value={draft.smtp_password}
                onChange={(e) => set({ smtp_password: e.target.value })}
                placeholder="leave blank to keep"
              />
            </label>
          </div>
          <button type="button" className="link" onClick={() => setShowSmtp((on) => !on)}>
            {showSmtp ? 'Hide' : 'Show'} the password
          </button>
        </div>
      )}

      <Provider
        name="Google"
        enabled={draft.google_enabled}
        onToggle={(on) => set({ google_enabled: on })}
        clientId={draft.google_client_id}
        onClientId={(v) => set({ google_client_id: v })}
        secret={draft.google_client_secret}
        onSecret={(v) => set({ google_client_secret: v })}
        redirectUri={redirectUri}
        addressMissing={addressMissing}
        where="Google Cloud console, under APIs & Services → Credentials"
      />

      <Provider
        name="Microsoft"
        enabled={draft.microsoft_enabled}
        onToggle={(on) => set({ microsoft_enabled: on })}
        clientId={draft.microsoft_client_id}
        onClientId={(v) => set({ microsoft_client_id: v })}
        secret={draft.microsoft_client_secret}
        onSecret={(v) => set({ microsoft_client_secret: v })}
        redirectUri={redirectUri}
        addressMissing={addressMissing}
        where="Azure portal, under App registrations"
      >
        <label>
          <span>Tenant</span>
          <input
            value={draft.microsoft_tenant}
            onChange={(e) => set({ microsoft_tenant: e.target.value })}
            placeholder="common"
          />
          <span className="hint">
            <code>common</code> lets any Microsoft account in. A tenant ID restricts it to
            your organization.
          </span>
        </label>
      </Provider>
    </div>
  )
}

function Provider({
  name,
  enabled,
  onToggle,
  clientId,
  onClientId,
  secret,
  onSecret,
  redirectUri,
  addressMissing,
  where,
  children,
}: {
  name: string
  enabled: boolean
  onToggle: (on: boolean) => void
  clientId: string
  onClientId: (v: string) => void
  secret: string
  onSecret: (v: string) => void
  redirectUri: string
  addressMissing?: boolean
  where: string
  children?: React.ReactNode
}) {
  return (
    <>
      <label className="check">
        <input type="checkbox" checked={enabled} onChange={(e) => onToggle(e.target.checked)} />
        <span>Sign in with {name}</span>
      </label>
      {enabled && (
        <div className="method-detail">
          <p className="hint">Create an OAuth client in the {where}.</p>
          <label>
            <span>Redirect URI — paste this into {name}</span>
            <input readOnly value={redirectUri} onFocus={(e) => e.target.select()} />
          </label>
          {addressMissing && (
            <p className="warn-note">
              Set the address above first. {name} will reject a redirect that does not
              match exactly, and this one is not final until the address is.
            </p>
          )}
          <label>
            <span>Client ID</span>
            <input value={clientId} onChange={(e) => onClientId(e.target.value)} />
          </label>
          <label>
            <span>Client secret</span>
            <input
              type="password"
              value={secret}
              onChange={(e) => onSecret(e.target.value)}
              placeholder="leave blank to keep the saved one"
            />
          </label>
          {children}
        </div>
      )}
    </>
  )
}
