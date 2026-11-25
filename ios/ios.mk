# The iOS merge gate: which schemes it runs and the targets the CI job calls
# (`make ios-project`, `make test-ios`), included by the Makefile. It lives under
# ios/ so the `ios` path filter runs iOS for any change to what iOS testing means,
# and an edit elsewhere in the Makefile does not (5 of 14 iOS runs, 2026-11, were
# triggered by Makefile edits that changed nothing here).

# Single source of truth for which iOS schemes run where. CI calls these targets
# rather than setting IOS_TEST_SCHEMES itself, so `make test-ios` and the merge
# gate cannot drift apart. They did, and every local run was green against a
# scheme set CI never ran. The one thing CI may set is how to split that set over
# VMs (IOS_TEST_SCHEMES and IOS_TEST_FILTER in the workflow matrix), and
# scripts/tests/ios-test-lanes.test.py fails if the lanes stop covering it.
IOS_MERGE_TEST_SCHEMES ?= Longhouse LonghouseSmoke
# The UI scheme's classes can run on IOS_PARALLEL_WORKERS simulator clones at
# once. On the 8 GB bench Mac two were slower than one: the UI tests took 537 s
# against 452 s, free memory fell to 26% and it swapped (run 45683709216 against
# 45681254244). Raise it on a Mac with more memory.
IOS_PARALLEL_SCHEMES ?= LonghouseSmoke
IOS_PARALLEL_WORKERS ?= 1

# The generated .xcodeproj is gitignored, so the committed SwiftPM pin file
# lives next to project.yml and is copied into the project on every generation.
IOS_PACKAGE_RESOLVED := ios/XcodeHarness/LonghouseIOS.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved

ios-project: ## Regenerate the local Xcode project and freshness stamp
	@python3 scripts/build/generate_build_identity.py
	@bash scripts/build/stage_ios_build_identity.sh
	@xcodegen --spec ios/XcodeHarness/project.yml --project-root ios/XcodeHarness
	@mkdir -p $(dir $(IOS_PACKAGE_RESOLVED))
	@cp ios/XcodeHarness/Package.resolved $(IOS_PACKAGE_RESOLVED)
	@python3 scripts/build/ios_project_stamp.py > ios/XcodeHarness/.project-source-sha256

ios-package-update: ## Re-resolve iOS Swift packages after changing a pin in project.yml, then commit Package.resolved
	@$(MAKE) ios-project
	@rm -f $(IOS_PACKAGE_RESOLVED)
	@xcodebuild -resolvePackageDependencies -project ios/XcodeHarness/LonghouseIOS.xcodeproj -scheme Longhouse
	@cp $(IOS_PACKAGE_RESOLVED) ios/XcodeHarness/Package.resolved

ios-project-check: ## Regenerate and verify Xcode source membership
	@$(MAKE) ios-project
	@scripts/build/check_ios_project_fresh.sh

test-ios: ## iOS unit + smoke tests (simulator), the merge gate (IOS_TEST_SCHEMES / IOS_TEST_FILTER run part of it)
	@$(MAKE) ios-project
	@DESTINATION="$${IOS_DESTINATION:-$$(python3 scripts/ci/select_ios_simulator.py ios/XcodeHarness/LonghouseIOS.xcodeproj Longhouse)}"; \
	if [ -n "$(IOS_TEST_SCHEMES)$(IOS_TEST_FILTER)" ]; then echo "test-ios: PARTIAL run (schemes '$(or $(IOS_TEST_SCHEMES),$(IOS_MERGE_TEST_SCHEMES))', filter '$(IOS_TEST_FILTER)'); the merge gate is '$(IOS_MERGE_TEST_SCHEMES)'"; fi; \
	IOS_TEST_SCHEMES="$(or $(IOS_TEST_SCHEMES),$(IOS_MERGE_TEST_SCHEMES))" IOS_PARALLEL_SCHEMES="$(IOS_PARALLEL_SCHEMES)" IOS_PARALLEL_WORKERS="$(IOS_PARALLEL_WORKERS)" ./scripts/ci/run_ios_tests.sh "$$DESTINATION"
