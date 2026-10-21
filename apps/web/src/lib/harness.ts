import type { HarnessInfo, HarnessKind } from './api'

/**
 * What the UI calls a harness, and the words its sign-in uses.
 *
 * `/api/harnesses` says what a harness is called and what it can do, but not
 * what its sign-in button should say or what its key looks like — those are
 * the vendor's own words, so they live here. Claude Code's entry is, word for
 * word, every string the UI used back when Claude was the only thing anyone
 * could connect: tests find controls by those labels, and so do people.
 *
 * A second harness used to inherit Claude's words wholesale. An Antigravity
 * project offered "Claude account" pickers full of Claude accounts, and
 * choosing one was refused with "No such account." because the backend checks
 * an account belongs to the project's harness. So anything that names an
 * account, the agent or its sign-in asks this for the project's own harness.
 */
export interface HarnessCopy {
  kind: string
  /** As `/api/harnesses` names it: "Claude Code", "Antigravity". */
  name: string
  /** The agent, in a sentence or on a feed row: "Claude", "Antigravity". */
  agent: string
  /** One of your accounts on it, as a picker's label says: "Claude account". */
  account: string
  /** What a signed-in account with no name of its own is called. */
  oauthAccount: string
  /** What an API-key account with no name of its own is called. */
  keyAccount: string
  signIn: string
  /** The first step of the paste-a-code sign-in. */
  openStep: string
  /** The second. */
  codeStep: string
  keyOption: string
  keyLabel: string
  keyPlaceholder: string
}

/**
 * Display names for when `/api/harnesses` has not answered yet, which is
 * every first render. The same names the backend's classes give themselves.
 */
const NAMES: Record<HarnessKind, string> = {
  claude_code: 'Claude Code',
  opencode: 'OpenCode',
  pydantic_ai: 'Pydantic AI',
  antigravity: 'Antigravity',
}

const OVERRIDES: Partial<Record<HarnessKind, Partial<HarnessCopy>>> = {
  claude_code: {
    agent: 'Claude',
    account: 'Claude account',
    oauthAccount: 'Claude subscription',
    signIn: 'Sign in with Claude',
    keyLabel: 'Anthropic API key',
    keyPlaceholder: 'sk-ant-…',
  },
  // The sign-in is Google's: agy prints an accounts.google.com link whose
  // redirect lands on a page at antigravity.google showing a code to copy
  // back — the same open-a-link, paste-a-code relay as Claude's. The key is
  // a Gemini API key; agy refuses GOOGLE_API_KEY by name.
  antigravity: {
    oauthAccount: 'Google account',
    keyAccount: 'Gemini API key',
    signIn: 'Sign in with Google',
    openStep: 'Open this URL and sign in with your Google account:',
    codeStep: 'Paste the code Google’s page shows you:',
    keyOption: 'Use a Gemini API key instead',
    keyLabel: 'Gemini API key',
    keyPlaceholder: 'AIza…',
  },
}

/**
 * The words for `kind`, preferring the display name `/api/harnesses` gave.
 *
 * A missing kind means Claude Code: it is what every project was before
 * projects had a harness, and what a fixture or an older answer without the
 * field still means.
 */
export function harnessCopy(
  kind?: string | null,
  harnesses?: HarnessInfo[] | null,
): HarnessCopy {
  const key = kind || 'claude_code'
  const name =
    harnesses?.find((h) => h.kind === key)?.display_name ?? NAMES[key as HarnessKind] ?? key
  return {
    kind: key,
    name,
    agent: name,
    account: `${name} account`,
    oauthAccount: `${name} sign-in`,
    keyAccount: 'API key',
    signIn: `Sign in to ${name}`,
    openStep: 'Open this URL and approve:',
    codeStep: 'Paste the code it gives you:',
    keyOption: 'Use an API key instead',
    keyLabel: 'API key',
    keyPlaceholder: '',
    ...OVERRIDES[key as HarnessKind],
  }
}
