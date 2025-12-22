/**
 * In-memory vector store with fast semantic search
 * Optimized for <5ms lookups during conversation
 */

import type { CompanyDocument, SearchResult } from './types'

export interface VectorStoreLogger {
  debug(message: string, data?: unknown): void
  performance(operation: string, duration: number, details?: unknown): void
}

const noopLogger: VectorStoreLogger = {
  debug: () => {},
  performance: () => {}
}

export class MemoryVectorStore {
  private documents: Map<string, CompanyDocument> = new Map()
  private initialized = false
  private logger: VectorStoreLogger

  constructor(logger: VectorStoreLogger = noopLogger) {
    this.logger = logger
  }

  async initialize(documents: CompanyDocument[]): Promise<void> {
    this.logger.debug(`Initializing MemoryVectorStore with ${documents.length} documents`)

    const startTime = performance.now()

    // Load all documents into memory
    this.documents.clear()
    for (const doc of documents) {
      this.documents.set(doc.id, doc)
    }

    const loadTime = performance.now() - startTime
    this.logger.performance('MemoryVectorStore loaded', loadTime)

    this.initialized = true
  }

  async semanticSearch(
    queryEmbedding: number[],
    options: {
      limit?: number
      minScore?: number
      type?: CompanyDocument['metadata']['type']
      category?: string
    } = {}
  ): Promise<SearchResult[]> {
    if (!this.initialized) {
      throw new Error('MemoryVectorStore not initialized')
    }

    const { limit = 5, minScore = 0.1, type, category } = options
    const startTime = performance.now()

    const results: SearchResult[] = []

    // Calculate cosine similarity for each document
    for (const [_id, document] of this.documents) {
      // Apply filters
      if (type && document.metadata.type !== type) continue
      if (category && document.metadata.category !== category) continue

      const score = this.cosineSimilarity(queryEmbedding, document.embedding)

      if (score >= minScore) {
        results.push({ document, score })
      }
    }

    // Sort by score (descending) and limit results
    results.sort((a, b) => b.score - a.score)
    const limitedResults = results.slice(0, limit)

    const searchTime = performance.now() - startTime
    this.logger.performance('Semantic search', searchTime, { resultsFound: limitedResults.length })

    return limitedResults
  }

  // Fast text-based search for testing without embeddings
  async textSearch(
    query: string,
    options: {
      limit?: number
      type?: CompanyDocument['metadata']['type']
      category?: string
    } = {}
  ): Promise<SearchResult[]> {
    if (!this.initialized) {
      throw new Error('MemoryVectorStore not initialized')
    }

    const { limit = 3, type, category } = options
    const startTime = performance.now()
    const queryLower = query.toLowerCase()

    const results: SearchResult[] = []

    // Simple text matching with relevance scoring
    for (const [_id, document] of this.documents) {
      // Apply filters
      if (type && document.metadata.type !== type) continue
      if (category && document.metadata.category !== category) continue

      const content = document.content.toLowerCase()

      // Calculate a simple relevance score based on term frequency and position
      let score = 0

      // Exact phrase match gets highest score
      if (content.includes(queryLower)) {
        score += 1.0
      }

      // Individual word matches
      const queryWords = queryLower.split(/\s+/)
      const contentWords = content.split(/\s+/)

      for (const word of queryWords) {
        const wordCount = contentWords.filter(w => w.includes(word)).length
        score += wordCount * 0.1
      }

      if (score > 0) {
        results.push({ document, score })
      }
    }

    // Sort by score (descending) and limit results
    results.sort((a, b) => b.score - a.score)
    const limitedResults = results.slice(0, limit)

    const searchTime = performance.now() - startTime
    this.logger.debug(`Text search completed`, { time: searchTime, resultsFound: limitedResults.length })

    return limitedResults
  }

  // Get documents by type or category
  async getDocuments(filters: {
    type?: CompanyDocument['metadata']['type']
    category?: string
  } = {}): Promise<CompanyDocument[]> {
    if (!this.initialized) return []

    const { type, category } = filters
    const results: CompanyDocument[] = []

    for (const [_id, document] of this.documents) {
      if (type && document.metadata.type !== type) continue
      if (category && document.metadata.category !== category) continue
      results.push(document)
    }

    return results
  }

  // Add or update a document
  async addDocument(document: CompanyDocument): Promise<void> {
    this.documents.set(document.id, document)
    this.logger.debug(`Added document ${document.id} to MemoryVectorStore`)
  }

  // Remove a document
  async removeDocument(documentId: string): Promise<void> {
    const removed = this.documents.delete(documentId)
    if (removed) {
      this.logger.debug(`Removed document ${documentId} from MemoryVectorStore`)
    }
  }

  // Get document by ID
  getDocument(id: string): CompanyDocument | undefined {
    return this.documents.get(id)
  }

  // Check if initialized
  isInitialized(): boolean {
    return this.initialized
  }

  // Get store statistics with memory usage
  getStats(): { documentCount: number, types: Record<string, number>, memoryUsageMB: number } {
    const types: Record<string, number> = {}
    let totalSize = 0

    for (const [_id, doc] of this.documents) {
      const type = doc.metadata.type
      types[type] = (types[type] || 0) + 1
      totalSize += JSON.stringify(doc).length * 2 // UTF-16 encoding
    }

    const memoryUsageMB = Math.round((totalSize / (1024 * 1024)) * 100) / 100

    return {
      documentCount: this.documents.size,
      types,
      memoryUsageMB
    }
  }

  // Clear all documents
  async clear(): Promise<void> {
    this.documents.clear()
    this.initialized = false
    this.logger.debug('MemoryVectorStore cleared')
  }

  // Calculate cosine similarity between two vectors
  private cosineSimilarity(a: number[], b: number[]): number {
    if (a.length !== b.length) {
      throw new Error('Vectors must have the same length')
    }

    let dotProduct = 0
    let normA = 0
    let normB = 0

    for (let i = 0; i < a.length; i++) {
      dotProduct += a[i] * b[i]
      normA += a[i] * a[i]
      normB += b[i] * b[i]
    }

    // Avoid division by zero
    const denominator = Math.sqrt(normA) * Math.sqrt(normB)
    if (denominator === 0) return 0

    return dotProduct / denominator
  }

  // Generate a mock embedding for testing (not for production use)
  generateMockEmbedding(text: string, dimensions = 1536): number[] {
    // Simple hash-based mock embedding for consistent testing
    let hash = 0
    for (let i = 0; i < text.length; i++) {
      const char = text.charCodeAt(i)
      hash = ((hash << 5) - hash) + char
      hash = hash & hash // Convert to 32-bit integer
    }

    const embedding: number[] = []
    const rng = this.mulberry32(Math.abs(hash))

    for (let i = 0; i < dimensions; i++) {
      embedding.push((rng() - 0.5) * 2) // Values between -1 and 1
    }

    return embedding
  }

  // Simple pseudo-random number generator for consistent mock embeddings
  private mulberry32(seed: number): () => number {
    return function() {
      let t = seed += 0x6D2B79F5
      t = Math.imul(t ^ t >>> 15, t | 1)
      t ^= t + Math.imul(t ^ t >>> 7, t | 61)
      return ((t ^ t >>> 14) >>> 0) / 4294967296
    }
  }
}
