<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, ApiError } from '../api/client'
import { useJobsStore } from '../stores/jobs'
import type {
  ConfigView,
  JobView,
  MetadataCandidate,
  MetadataSearchResponse,
  MetadataSearchType,
  ResolveResponse,
} from '../api/types'

const props = defineProps<{ job: JobView }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'identified', resp: ResolveResponse): void
  (e: 'ripStarted', job: JobView): void
  (e: 'jobUpdated', job: JobView): void
}>()

const jobs = useJobsStore()

// `isCd` switches the form body between the two-field (title + year)
// video shape and the structured-music shape. The music path template
// requires `{artist}`, `{album}`, and per-track `{track_title}` — none
// of which a title-only resolve can populate.
const isCd = computed(() => props.job.disc_type === 'cd')

// `isEditMode` distinguishes the "auto-identify failed, please fill in"
// case (status awaiting_user_id / ripped_awaiting_identify) from the
// "auto-identify landed wrong metadata, correct it" case (post-rip
// status). The submit endpoint is the same; only the copy differs so
// the user understands which scenario they're in.
const isEditMode = computed(
  () => props.job.status !== 'awaiting_user_id' && props.job.status !== 'ripped_awaiting_identify',
)

// CD-only: per-track count comes from the preserved scan_result on the
// job's metadata_json (the identify endpoint preserves scan_result
// across the resolve flow). If the scan_result is somehow absent (e.g.
// older job, manual seed), we skip the per-track inputs and show a
// helper line; the resolve still succeeds but `track_title` will
// resolve empty when transcode tasks fan out.
const scanTrackCount = computed<number>(() => {
  const titles = (props.job.metadata_json?.scan_result as { titles?: unknown[] } | undefined)
    ?.titles
  return Array.isArray(titles) ? titles.length : 0
})

// Video / DVD / BD / data fields.
const title = ref<string>(props.job.title ?? '')
const year = ref<number | null>(props.job.year)

// CD fields.
const album = ref<string>(props.job.title ?? '')
const artist = ref<string>('')
const trackTitles = ref<string[]>(Array.from({ length: scanTrackCount.value }, () => ''))

const submitting = ref(false)
const error = ref<string | null>(null)

// `awaiting_review` means auto-identify already ran (or a resolve() promoted
// the job here) and the pipeline is parked waiting for an explicit confirm —
// per the mandatory/timed review-gate design. Distinct from isEditMode, which
// only distinguishes copy for the identify-failed vs correct-existing cases.
const isReview = computed(() => props.job.status === 'awaiting_review')

// Manual search + retry, mitigating volume-label search-noise misses (e.g. a
// disc/volume-set token making the auto query miss TMDB entirely) — available
// regardless of review state, on the video path only (CD uses a structured
// artist/album/track shape the candidate search doesn't return).
const searchTitle = ref<string>(props.job.title ?? '')
const searchType = ref<MetadataSearchType>('movie')
const searching = ref(false)
const searchError = ref<string | null>(null)
const candidates = ref<MetadataCandidate[]>([])
const pickedCandidate = ref<MetadataCandidate | null>(null)

async function doSearch(): Promise<void> {
  const q = searchTitle.value.trim()
  if (!q) return
  searching.value = true
  searchError.value = null
  try {
    const resp = await api.get<MetadataSearchResponse>(
      `/api/metadata/search?title=${encodeURIComponent(q)}&type=${searchType.value}`,
    )
    candidates.value = resp.candidates
    searchError.value = resp.candidates.length === 0 ? (resp.detail ?? 'No results') : null
  } catch (e) {
    candidates.value = []
    searchError.value = e instanceof ApiError ? e.message : 'Search failed'
  } finally {
    searching.value = false
  }
}

function pickCandidate(c: MetadataCandidate): void {
  title.value = c.title
  year.value = c.year
  pickedCandidate.value = c
}

// Review-gate controls (only relevant when isReview). Config is a singleton,
// so a global fetch tells us whether this install is in timed or mandatory
// sub-mode — the job itself doesn't carry that distinction.
const reviewConfig = ref<ConfigView | null>(null)
const isTimedMode = computed(() => reviewConfig.value?.manual_wait_seconds != null)
const startingRip = ref(false)
const pausing = ref(false)

if (isReview.value) {
  api
    .get<ConfigView>('/api/config')
    .then((c) => {
      reviewConfig.value = c
    })
    .catch(() => {
      // best-effort; the pause control simply won't render on failure
    })
}

async function confirmAndStartRip(): Promise<void> {
  startingRip.value = true
  error.value = null
  try {
    const updated = await jobs.ripStartReview(props.job.id)
    emit('ripStarted', updated)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to start rip'
  } finally {
    startingRip.value = false
  }
}

async function togglePause(): Promise<void> {
  pausing.value = true
  error.value = null
  try {
    const updated = await jobs.reviewPause(props.job.id, !props.job.manual_pause)
    emit('jobUpdated', updated)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Failed to update pause state'
  } finally {
    pausing.value = false
  }
}

const canSubmit = computed<boolean>(() => {
  if (submitting.value) return false
  if (isCd.value) {
    return album.value.trim().length > 0 && artist.value.trim().length > 0
  }
  return title.value.trim().length > 0
})

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = null
  try {
    const payload = isCd.value
      ? {
          title: album.value.trim(),
          year: year.value ?? null,
          metadata: {
            artist: artist.value.trim(),
            album: album.value.trim(),
            tracks: trackTitles.value.map((t) => ({ title: t.trim() })),
          },
        }
      : {
          title: title.value.trim(),
          year: year.value ?? null,
          ...(pickedCandidate.value && pickedCandidate.value.title === title.value.trim()
            ? {
                metadata: {
                  provider_id: pickedCandidate.value.provider_id,
                  poster_url: pickedCandidate.value.poster_url,
                  kind: pickedCandidate.value.kind,
                },
              }
            : {}),
        }
    const resp = await jobs.resolve(props.job.id, payload)
    emit('identified', resp)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Identify failed'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="card" style="max-width: 560px; margin-top: 16px">
    <h3 style="margin-top: 0">
      {{
        isReview ? 'Review & confirm identity' : isEditMode ? 'Edit identity' : 'Identify this disc'
      }}
    </h3>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="isReview">
      This disc was identified automatically. Confirm the details below (or search / override them
      first) and click <strong>Confirm &amp; start rip</strong> to begin ripping.
    </p>
    <p v-else-if="isEditMode">
      Update the title, year, and metadata for this job. Status stays as-is. Existing transcoded
      files keep their original filenames — re-apply a session if you want new outputs under the
      corrected name.
    </p>
    <p v-else>
      The disc on drive <code>{{ job.drive_id }}</code> couldn't be identified automatically. Fill
      in the details so ARM can proceed. Any session you've already applied will pick up the
      resolved metadata and queue its transcode tasks.
    </p>
    <div v-if="!isCd" class="card" style="margin-bottom: 12px">
      <div class="row" style="gap: 8px; align-items: flex-end; flex-wrap: wrap">
        <label style="flex: 2 1 160px">
          Search title
          <input
            v-model="searchTitle"
            type="text"
            data-testid="search-title"
            :disabled="submitting || searching"
            style="width: 100%"
            @keydown.enter.prevent="doSearch"
          />
        </label>
        <label style="flex: 1 1 100px">
          Type
          <select
            v-model="searchType"
            data-testid="search-type"
            :disabled="submitting || searching"
          >
            <option value="movie">Movie</option>
            <option value="tv">TV</option>
          </select>
        </label>
        <button
          type="button"
          data-testid="search-run"
          :disabled="searching || submitting || !searchTitle.trim()"
          @click="doSearch"
        >
          {{ searching ? 'Searching…' : 'Search' }}
        </button>
      </div>
      <p v-if="searchError" class="error" style="margin-bottom: 0">{{ searchError }}</p>
      <div
        v-if="candidates.length > 0"
        class="row"
        style="gap: 8px; flex-wrap: wrap; margin-top: 8px"
      >
        <button
          v-for="c in candidates"
          :key="`${c.provider_id}-${c.title}-${c.year}`"
          type="button"
          class="secondary"
          :data-testid="`identify-candidate-${c.provider_id}`"
          style="
            display: flex;
            flex-direction: column;
            align-items: center;
            width: 100px;
            padding: 4px;
          "
          @click="pickCandidate(c)"
        >
          <img
            v-if="c.poster_url"
            :src="c.poster_url"
            :alt="c.title"
            style="width: 100%; aspect-ratio: 2 / 3; object-fit: cover; border-radius: 2px"
          />
          <span v-else style="font-size: 32px">🎬</span>
          <span style="font-size: 12px; margin-top: 4px; text-align: center"
            >{{ c.title }}<template v-if="c.year"> ({{ c.year }})</template></span
          >
        </button>
      </div>
    </div>
    <form @submit.prevent="submit">
      <template v-if="isCd">
        <div class="row" style="margin-bottom: 12px; gap: 8px">
          <label style="flex: 2 1 0">
            Album
            <input
              v-model="album"
              type="text"
              required
              data-testid="identify-album"
              :disabled="submitting"
              style="width: 100%"
            />
          </label>
          <label style="flex: 1 1 0">
            Year
            <input
              v-model.number="year"
              type="number"
              min="1888"
              max="2100"
              data-testid="identify-year"
              :disabled="submitting"
              style="width: 100%"
            />
          </label>
        </div>
        <div class="row" style="margin-bottom: 12px">
          <label style="flex: 1 1 0">
            Artist
            <input
              v-model="artist"
              type="text"
              required
              data-testid="identify-artist"
              :disabled="submitting"
              style="width: 100%"
            />
          </label>
        </div>
        <div v-if="scanTrackCount > 0" style="margin-bottom: 12px">
          <div class="muted" style="margin-bottom: 4px">Track titles</div>
          <div
            v-for="(_t, idx) in trackTitles"
            :key="idx"
            class="row"
            style="margin-bottom: 4px; gap: 8px; align-items: center"
          >
            <span class="muted" style="width: 32px; text-align: right">{{
              String(idx + 1).padStart(2, '0')
            }}</span>
            <input
              v-model="trackTitles[idx]"
              type="text"
              :data-testid="`identify-track-${idx + 1}`"
              :disabled="submitting"
              style="flex: 1"
            />
          </div>
        </div>
        <p v-else class="muted" style="margin-bottom: 12px">
          Track count couldn't be determined from the scan; transcoded filenames will fall back to
          generic names.
        </p>
      </template>
      <template v-else>
        <div class="row" style="margin-bottom: 12px; gap: 8px">
          <label style="flex: 2 1 0">
            Title
            <input
              v-model="title"
              type="text"
              required
              data-testid="identify-title"
              :disabled="submitting"
              style="width: 100%"
            />
          </label>
          <label style="flex: 1 1 0">
            Year
            <input
              v-model.number="year"
              type="number"
              min="1888"
              max="2100"
              data-testid="identify-year"
              :disabled="submitting"
              style="width: 100%"
            />
          </label>
        </div>
      </template>
      <div class="row" style="gap: 8px">
        <button type="submit" :disabled="!canSubmit" data-testid="identify-submit">
          {{ submitting ? 'Saving…' : isEditMode ? 'Save' : 'Identify' }}
        </button>
        <button type="button" class="secondary" :disabled="submitting" @click="emit('close')">
          Cancel
        </button>
      </div>
    </form>
    <div v-if="isReview" class="row" style="gap: 8px; margin-top: 12px; align-items: center">
      <button
        type="button"
        data-testid="confirm-start-rip"
        :disabled="startingRip"
        @click="confirmAndStartRip"
      >
        {{ startingRip ? 'Starting…' : 'Confirm & start rip' }}
      </button>
      <template v-if="isTimedMode">
        <button
          type="button"
          class="secondary"
          data-testid="review-pause-toggle"
          :disabled="pausing"
          @click="togglePause"
        >
          {{ job.manual_pause ? 'Resume countdown' : 'Pause countdown' }}
        </button>
        <span v-if="job.manual_pause" class="muted">Countdown paused</span>
      </template>
    </div>
  </div>
</template>
