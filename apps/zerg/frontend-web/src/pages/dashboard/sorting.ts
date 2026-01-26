import type { FicheSummary, Course } from "../../services/api";
import { formatDateTimeShort } from "./formatters";

export type SortKey = "name" | "status" | "created_at" | "last_course" | "next_course" | "success";

export type SortConfig = {
  key: SortKey;
  ascending: boolean;
};

export type FicheCoursesState = Record<number, Course[]>;

const STATUS_ORDER: Record<string, number> = {
  running: 0,
  processing: 1,
  idle: 2,
  error: 3,
};

const STORAGE_KEY_SORT = "dashboard_sort_key";
const STORAGE_KEY_ASC = "dashboard_sort_asc";

export function loadSortConfig(): SortConfig {
  if (typeof window === "undefined") {
    return { key: "name", ascending: true };
  }

  const storedKey = window.localStorage.getItem(STORAGE_KEY_SORT) ?? "name";
  const storedAsc = window.localStorage.getItem(STORAGE_KEY_ASC);

  const keyMap: Record<string, SortKey> = {
    name: "name",
    status: "status",
    created_at: "created_at",
    last_course: "last_course",
    next_course: "next_course",
    success: "success",
  };

  const key = keyMap[storedKey] ?? "name";
  const ascending = storedAsc === null ? true : storedAsc !== "0";
  return { key, ascending };
}

export function persistSortConfig(config: SortConfig) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(STORAGE_KEY_SORT, config.key);
  window.localStorage.setItem(STORAGE_KEY_ASC, config.ascending ? "1" : "0");
}

export function sortFiches(fiches: FicheSummary[], coursesByFiche: FicheCoursesState, sortConfig: SortConfig): FicheSummary[] {
  const sorted = [...fiches];
  sorted.sort((left, right) => {
    const comparison = compareFiches(left, right, coursesByFiche, sortConfig.key);
    if (comparison !== 0) {
      return sortConfig.ascending ? comparison : -comparison;
    }
    const fallback = left.name.toLowerCase().localeCompare(right.name.toLowerCase());
    return sortConfig.ascending ? fallback : -fallback;
  });
  return sorted;
}

function compareFiches(
  left: FicheSummary,
  right: FicheSummary,
  coursesByFiche: FicheCoursesState,
  sortKey: SortKey
): number {
  switch (sortKey) {
    case "name":
      return left.name.toLowerCase().localeCompare(right.name.toLowerCase());
    case "status":
      return (STATUS_ORDER[left.status] ?? 99) - (STATUS_ORDER[right.status] ?? 99);
    case "created_at":
      return formatDateTimeShort(left.created_at ?? null).localeCompare(
        formatDateTimeShort(right.created_at ?? null)
      );
    case "last_course":
      return formatDateTimeShort(left.last_course_at ?? null).localeCompare(
        formatDateTimeShort(right.last_course_at ?? null)
      );
    case "next_course":
      return formatDateTimeShort(left.next_course_at ?? null).localeCompare(
        formatDateTimeShort(right.next_course_at ?? null)
      );
    case "success": {
      const leftStats = computeCourseSuccessStats(coursesByFiche[left.id]);
      const rightStats = computeCourseSuccessStats(coursesByFiche[right.id]);
      if (leftStats.rate === rightStats.rate) {
        return leftStats.count - rightStats.count;
      }
      return leftStats.rate - rightStats.rate;
    }
    default:
      return 0;
  }
}

export function computeCourseSuccessStats(courses?: Course[]): { display: string; rate: number; count: number } {
  if (!courses || courses.length === 0) {
    return { display: "0.0% (0)", rate: 0, count: 0 };
  }

  const successCount = courses.filter((course) => course.status === "success").length;
  const successRate = courses.length === 0 ? 0 : (successCount / courses.length) * 100;
  return {
    display: `${successRate.toFixed(1)}% (${courses.length})`,
    rate: successRate,
    count: courses.length,
  };
}

export function determineLastCourseIndicator(courses?: Course[]): boolean | null {
  if (!courses || courses.length === 0) {
    return null;
  }
  const status = courses[0]?.status;
  if (status === "success") {
    return true;
  }
  if (status === "failed") {
    return false;
  }
  return null;
}
