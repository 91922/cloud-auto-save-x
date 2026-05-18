import { http } from '@/api/http'
import type { DoubanCategoryList, MediaDiscoverList, TMDBDetail, TMDBSearchList } from '@/types/media'

export async function fetchDoubanCategories() {
  const response = await http.get<DoubanCategoryList>('/media/douban/categories')
  return response.data
}

export async function fetchDoubanList(params: { main_category: string; sub_category?: string; start?: number; limit?: number }) {
  const response = await http.get<MediaDiscoverList>('/media/douban/list', { params })
  return response.data
}

export async function searchTMDB(params: { q: string; type?: 'multi' | 'movie' | 'tv'; page?: number; year?: string }) {
  const response = await http.get<TMDBSearchList>('/media/search', { params })
  return response.data
}

export async function fetchTMDBDetail(mediaType: 'movie' | 'tv', id: number) {
  const response = await http.get<TMDBDetail>(`/media/${mediaType}/${id}`)
  return response.data
}
