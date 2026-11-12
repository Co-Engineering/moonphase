import { describe, expect, it } from 'vitest'
import { projectBytes } from '../App'

describe('a project’s share of its server’s disk', () => {
  it('counts what its container wrote outside its volumes', () => {
    // Clockwork's /tmp held 20 GB and its bar showed only the volumes.
    expect(
      projectBytes({
        project_id: 'p',
        name: 'clockwork',
        workspace_bytes: 7e9,
        home_bytes: 43e9,
        container_bytes: 26e9,
        cpu_percent: null,
        mem_bytes: null,
      }),
    ).toBe(76e9)
  })

  it('is the volumes alone in a reading from before containers were counted', () => {
    expect(
      projectBytes({
        project_id: 'p',
        name: 'x',
        workspace_bytes: 1,
        home_bytes: 2,
        cpu_percent: null,
        mem_bytes: null,
      }),
    ).toBe(3)
  })
})
