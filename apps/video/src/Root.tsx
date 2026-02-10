import { Composition } from "remotion";
import { TimelineDemo } from "./compositions/TimelineDemo";
import { HookScene } from "./scenes/HookScene";
import { SearchWowScene } from "./scenes/SearchWowScene";
import { TimelineExploreScene } from "./scenes/TimelineExploreScene";
import { SessionDetailScene } from "./scenes/SessionDetailScene";
import { CtaScene } from "./scenes/CtaScene";
import { FPS, WIDTH, HEIGHT, SCENE_FRAMES, TOTAL_FRAMES } from "./lib/timing";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/* Full composition */}
      <Composition
        id="TimelineDemo"
        component={TimelineDemo}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />

      {/* Individual scene previews */}
      <Composition
        id="HookScene"
        component={HookScene}
        durationInFrames={SCENE_FRAMES.hook}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
      <Composition
        id="SearchWowScene"
        component={SearchWowScene}
        durationInFrames={SCENE_FRAMES.searchWow}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
      <Composition
        id="TimelineExploreScene"
        component={TimelineExploreScene}
        durationInFrames={SCENE_FRAMES.timelineExplore}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
      <Composition
        id="SessionDetailScene"
        component={SessionDetailScene}
        durationInFrames={SCENE_FRAMES.sessionDetail}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
      <Composition
        id="CtaScene"
        component={CtaScene}
        durationInFrames={SCENE_FRAMES.cta}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
    </>
  );
};
