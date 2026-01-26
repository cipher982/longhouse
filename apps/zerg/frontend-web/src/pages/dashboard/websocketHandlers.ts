import type { DashboardSnapshot, FicheSummary, Course } from "../../services/api";

export function applyFicheStateUpdate(
  current: DashboardSnapshot,
  ficheId: number,
  dataPayload: Record<string, unknown>
): DashboardSnapshot {
  const validStatuses = ["idle", "running", "processing", "error"] as const;
  const statusValue =
    typeof dataPayload.status === "string" && validStatuses.includes(dataPayload.status as (typeof validStatuses)[number])
      ? (dataPayload.status as FicheSummary["status"])
      : undefined;
  const lastCourseAtValue = typeof dataPayload.last_course_at === "string" ? dataPayload.last_course_at : undefined;
  const nextCourseAtValue = typeof dataPayload.next_course_at === "string" ? dataPayload.next_course_at : undefined;
  const lastErrorValue =
    dataPayload.last_error === null || typeof dataPayload.last_error === "string"
      ? (dataPayload.last_error as string | null)
      : undefined;

  let changed = false;
  const nextFiches = current.fiches.map((fiche) => {
    if (fiche.id !== ficheId) {
      return fiche;
    }

    const nextFiche: FicheSummary = {
      ...fiche,
      status: statusValue ?? fiche.status,
      last_course_at: lastCourseAtValue ?? fiche.last_course_at,
      next_course_at: nextCourseAtValue ?? fiche.next_course_at,
      last_error: lastErrorValue !== undefined ? lastErrorValue : fiche.last_error,
    };

    if (
      nextFiche.status !== fiche.status ||
      nextFiche.last_course_at !== fiche.last_course_at ||
      nextFiche.next_course_at !== fiche.next_course_at ||
      nextFiche.last_error !== fiche.last_error
    ) {
      changed = true;
      return nextFiche;
    }
    return fiche;
  });

  if (!changed) {
    return current;
  }

  return {
    ...current,
    fiches: nextFiches,
  };
}

export function applyCourseUpdate(
  current: DashboardSnapshot,
  ficheId: number,
  dataPayload: Record<string, unknown>
): DashboardSnapshot {
  const courseIdCandidate = dataPayload.id ?? dataPayload.course_id;
  const courseId = typeof courseIdCandidate === "number" ? courseIdCandidate : null;
  if (courseId == null) {
    return current;
  }

  const threadId =
    typeof dataPayload.thread_id === "number" ? (dataPayload.thread_id as number) : undefined;

  const courseBundles = current.courses.slice();
  let bundleIndex = courseBundles.findIndex((bundle) => bundle.ficheId === ficheId);
  let coursesChanged = false;

  if (bundleIndex === -1) {
    courseBundles.push({ ficheId, courses: [] });
    bundleIndex = courseBundles.length - 1;
    coursesChanged = true;
  }

  const targetBundle = courseBundles[bundleIndex];
  const existingCourses = targetBundle.courses ?? [];
  const existingIndex = existingCourses.findIndex((course) => course.id === courseId);
  let nextCourses = existingCourses;

  if (existingIndex === -1) {
    if (threadId === undefined) {
      return current;
    }

    const newCourse: Course = {
      id: courseId,
      fiche_id: ficheId,
      thread_id: threadId,
      status:
        typeof dataPayload.status === "string"
          ? (dataPayload.status as Course["status"])
          : "running",
      trigger:
        typeof dataPayload.trigger === "string"
          ? (dataPayload.trigger as Course["trigger"])
          : "manual",
      started_at: typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : null,
      finished_at: typeof dataPayload.finished_at === "string" ? (dataPayload.finished_at as string) : null,
      duration_ms: typeof dataPayload.duration_ms === "number" ? (dataPayload.duration_ms as number) : null,
      total_tokens: typeof dataPayload.total_tokens === "number" ? (dataPayload.total_tokens as number) : null,
      total_cost_usd:
        typeof dataPayload.total_cost_usd === "number" ? (dataPayload.total_cost_usd as number) : null,
      error:
        dataPayload.error === undefined
          ? null
          : (dataPayload.error as string | null) ?? null,
      display_type:
        typeof dataPayload.display_type === "string" ? (dataPayload.display_type as string) : "course",
    };

    nextCourses = [newCourse, ...existingCourses];
    if (nextCourses.length > current.coursesLimit) {
      nextCourses = nextCourses.slice(0, current.coursesLimit);
    }
    coursesChanged = true;
  } else {
    const previousCourse = existingCourses[existingIndex];
    const updatedCourse: Course = {
      ...previousCourse,
      status:
        typeof dataPayload.status === "string"
          ? (dataPayload.status as Course["status"])
          : previousCourse.status,
      started_at:
        typeof dataPayload.started_at === "string"
          ? (dataPayload.started_at as Course["started_at"])
          : previousCourse.started_at,
      finished_at:
        typeof dataPayload.finished_at === "string"
          ? (dataPayload.finished_at as Course["finished_at"])
          : previousCourse.finished_at,
      duration_ms:
        typeof dataPayload.duration_ms === "number"
          ? (dataPayload.duration_ms as Course["duration_ms"])
          : previousCourse.duration_ms,
      total_tokens:
        typeof dataPayload.total_tokens === "number"
          ? (dataPayload.total_tokens as Course["total_tokens"])
          : previousCourse.total_tokens,
      total_cost_usd:
        typeof dataPayload.total_cost_usd === "number"
          ? (dataPayload.total_cost_usd as Course["total_cost_usd"])
          : previousCourse.total_cost_usd,
      error:
        dataPayload.error === undefined
          ? previousCourse.error
          : ((dataPayload.error as string | null) ?? null),
    };

    const hasCourseDiff =
      updatedCourse.status !== previousCourse.status ||
      updatedCourse.started_at !== previousCourse.started_at ||
      updatedCourse.finished_at !== previousCourse.finished_at ||
      updatedCourse.duration_ms !== previousCourse.duration_ms ||
      updatedCourse.total_tokens !== previousCourse.total_tokens ||
      updatedCourse.total_cost_usd !== previousCourse.total_cost_usd ||
      updatedCourse.error !== previousCourse.error;

    if (hasCourseDiff) {
      nextCourses = [...existingCourses];
      nextCourses[existingIndex] = updatedCourse;
      coursesChanged = true;
    }
  }

  if (coursesChanged) {
    courseBundles[bundleIndex] = {
      ficheId,
      courses: nextCourses,
    };
  }

  let fichesChanged = false;
  const validFicheStatuses = ["idle", "running", "processing", "error"] as const;
  const updatedFiches = current.fiches.map((fiche) => {
    if (fiche.id !== ficheId) {
      return fiche;
    }

    const statusValue =
      typeof dataPayload.status === "string" && validFicheStatuses.includes(dataPayload.status as (typeof validFicheStatuses)[number])
        ? (dataPayload.status as FicheSummary["status"])
        : fiche.status;
    const lastCourseValue =
      typeof dataPayload.started_at === "string" ? (dataPayload.started_at as string) : fiche.last_course_at;

    if (statusValue === fiche.status && lastCourseValue === fiche.last_course_at) {
      return fiche;
    }

    fichesChanged = true;
    return {
      ...fiche,
      status: statusValue,
      last_course_at: lastCourseValue,
    };
  });

  if (!coursesChanged && !fichesChanged) {
    return current;
  }

  return {
    ...current,
    fiches: fichesChanged ? updatedFiches : current.fiches,
    courses: coursesChanged ? courseBundles : current.courses,
  };
}
