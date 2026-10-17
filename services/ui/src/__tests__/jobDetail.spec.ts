import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import JobDetail from '../views/JobDetail.vue'
import ApplySessionDialog from '../components/ApplySessionDialog.vue'
import type { JobStatus } from '../api/types'
import { wsClient } from '../api/ws'

const baseJob = {
  id: 'job_x',
  drive_id: 'drv_x',
  disc_type: 'dvd',
  status: 'identified' as JobStatus,
  title: 'Iron Man',
  year: 2008,
  poster_url: null,
  poster_url_manual: null,
  metadata_json: {},
  resumed_from_crash: false,
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function detailFor(status: JobStatus, tracks: unknown[] = []): unknown {
  return {
    job: { ...baseJob, status },
    tracks,
  }
}

function track(index: number, fields: Record<string, unknown> = {}): unknown {
  return {
    id: `trk_${index}`,
    job_id: 'job_x',
    kind: 'video_title',
    index,
    source_ref: String(index),
    status: 'queued',
    output_path: null,
    size_bytes: null,
    expected_size_bytes: null,
    duration_seconds: null,
    expected_duration_seconds: null,
    attempts: 0,
    last_error: null,
    ...fields,
  }
}

function ndjsonResponse(records: unknown[]): Response {
  const body = records.map((r) => JSON.stringify(r)).join('\n') + '\n'
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'application/x-ndjson' },
  })
}

async function mountWithStatus(status: JobStatus, tracks: unknown[] = []) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/jobs/:id', name: 'job', component: JobDetail },
      // The pre-rip "use + Manual rip instead" hint links here.
      { path: '/jobs/manual', name: 'job-manual', component: { template: '<div />' } },
    ],
  })
  await router.push('/jobs/job_x')
  await router.isReady()
  // load() calls /api/jobs/:id, then transcodes.fetchAll() hits /api/transcodes.
  // JobLogsCard fetches /api/logs/:id (ndjson) and subscribes to a WS topic.
  // Stub everything: real WS would blow up under jsdom (no WebSocket).
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/api/jobs/')) return Promise.resolve(jsonResponse(detailFor(status, tracks)))
    if (url.includes('/api/transcodes')) return Promise.resolve(jsonResponse([]))
    // ApplySessionDialog loads the session list as soon as it mounts.
    if (url.includes('/api/sessions')) return Promise.resolve(jsonResponse([]))
    if (url.includes('/api/logs/')) return Promise.resolve(ndjsonResponse([]))
    return Promise.resolve(jsonResponse(null))
  })
  vi.stubGlobal('fetch', fetchMock)
  vi.spyOn(wsClient, 'start').mockImplementation(() => {})
  vi.spyOn(wsClient, 'subscribe').mockImplementation(() => () => {})
  const wrapper = mount(JobDetail, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('JobDetail.vue identify-disc button', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    localStorage.setItem('arm_token', 'aaa.bbb.ccc')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it.each<[JobStatus, string]>([
    ['awaiting_user_id', 'Identify disc'],
    ['ripped_awaiting_identify', 'Identify disc'],
    ['identified', 'Edit identity'],
    ['ripped', 'Edit identity'],
    ['ripped_partial', 'Edit identity'],
  ])('shows the identify button labelled "%s" when status is %s', async (status, expectedLabel) => {
    const wrapper = await mountWithStatus(status)
    const btn = wrapper.find('[data-testid="identify-disc"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toBe(expectedLabel)
  })

  it.each<JobStatus>(['created', 'ripping', 'abandoned', 'failed'])(
    'hides the identify button when status is %s',
    async (status) => {
      const wrapper = await mountWithStatus(status)
      expect(wrapper.find('[data-testid="identify-disc"]').exists()).toBe(false)
    },
  )
})

describe('JobDetail.vue apply-session button', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    localStorage.setItem('arm_token', 'aaa.bbb.ccc')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it.each<JobStatus>(['ripped', 'ripped_partial', 'awaiting_user_id', 'awaiting_review'])(
    'offers Apply session when status is %s',
    async (status) => {
      const wrapper = await mountWithStatus(status)
      expect(wrapper.find('[data-testid="apply-session"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="apply-after-rip"]').exists()).toBe(false)
    },
  )

  it('points a pre-rip identified job at "+ Manual rip" instead of Apply', async () => {
    // The backend refuses apply on `identified`: fanned-out tasks would have no
    // ripped file to read, so the session is chosen at manual-trigger time.
    const wrapper = await mountWithStatus('identified')
    expect(wrapper.find('[data-testid="apply-session"]').exists()).toBe(false)
    const hint = wrapper.find('[data-testid="apply-after-rip"]')
    expect(hint.exists()).toBe(true)
    expect(hint.attributes('href')).toBe('/jobs/manual')
  })
})

describe('JobDetail.vue apply-session result card', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    localStorage.setItem('arm_token', 'aaa.bbb.ccc')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  async function applyOn(status: JobStatus, applicationStatus: string) {
    const wrapper = await mountWithStatus(status)
    await wrapper.find('[data-testid="apply-session"]').trigger('click')
    await flushPromises()
    wrapper.findComponent(ApplySessionDialog).vm.$emit('applied', {
      session_application: { id: 'sap_x', status: applicationStatus },
      tasks: [],
      idempotent: false,
    })
    await flushPromises()
    return wrapper
  }

  it('explains that a held disc parks until its gate clears', async () => {
    const wrapper = await applyOn('awaiting_review', 'waiting_identify')
    expect(wrapper.text()).toContain('Session parked')
    expect(wrapper.find('[data-testid="apply-parked-note"]').exists()).toBe(true)
  })

  it('reports a queued session as queued', async () => {
    const wrapper = await applyOn('ripped', 'queued')
    expect(wrapper.text()).toContain('Session queued')
    expect(wrapper.find('[data-testid="apply-parked-note"]').exists()).toBe(false)
  })
})

describe('JobDetail.vue tracks table', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    localStorage.setItem('arm_token', 'aaa.bbb.ccc')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("shows each track's duration: the actual, else the ~estimate, else a dash", async () => {
    const wrapper = await mountWithStatus('ripped', [
      track(0, { expected_duration_seconds: 1452 }),
      track(1, { duration_seconds: 13078, expected_duration_seconds: 13000 }),
      track(2),
    ])
    const cells = wrapper.findAll('[data-testid="track-duration"]').map((c) => c.text())
    expect(cells).toEqual(['~24:12', '3:37:58', '—'])
  })
})
