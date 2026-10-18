<script setup lang="ts">
import { ref } from 'vue'
import { ApiError } from '../api/client'
import { useJobsStore } from '../stores/jobs'
import type { JobView, RawBackupView } from '../api/types'
import { formatSizeGb } from '../utils/size'

const props = defineProps<{ job: JobView; backup: RawBackupView }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'deleted', jobId: string): void
}>()

const jobs = useJobsStore()
const submitting = ref(false)
const error = ref<string | null>(null)

async function confirm(): Promise<void> {
  submitting.value = true
  error.value = null
  try {
    await jobs.deleteBackup(props.job.id)
    emit('deleted', props.job.id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Delete failed'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="card" style="max-width: 560px; margin-top: 16px">
    <h3 style="margin-top: 0">Delete the raw backup?</h3>
    <p v-if="error" class="error">{{ error }}</p>
    <p>
      The disc backup at <code>/raw/{{ job.id }}/.backup/</code> ({{
        formatSizeGb(backup.size_bytes)
      }}) will be deleted. The ripped files and the job stay. Titles that failed to rip can then
      only be retried by reading the disc again.
    </p>
    <div class="row" style="gap: 8px">
      <button
        :disabled="submitting"
        data-testid="delete-raw-backup-confirm"
        type="button"
        @click="confirm"
      >
        {{ submitting ? 'Deleting…' : 'Delete raw backup' }}
      </button>
      <button class="secondary" :disabled="submitting" type="button" @click="emit('close')">
        Cancel
      </button>
    </div>
  </div>
</template>
