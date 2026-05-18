import { http } from '@/api/http'
import type { ResourceSearchSourceKey, ResourceSearchSourceListResponse, TaskSuggestionResponse } from '@/types/resourceSearch'

export async function fetchResourceSearchSources() {
  const { data } = await http.get<ResourceSearchSourceListResponse>('/resource-search/sources')
  return data
}

export async function patchResourceSearchSource(key: ResourceSearchSourceKey, payload: Partial<{ enabled: boolean; server: string | null; username: string | null; password: string | null; token: string | null }>) {
  const { data } = await http.patch<ResourceSearchSourceListResponse>(`/resource-search/sources/${encodeURIComponent(key)}`, payload)
  return data
}

export async function fetchTaskSuggestions(q: string, d: number) {
  const { data } = await http.get<TaskSuggestionResponse>('/tasks/suggestions', { params: { q, d }, timeout: 60000 })
  return data
}
