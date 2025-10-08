import StreamStart from './StreamStart';
import StreamChunk from './StreamChunk';
import StreamEnd from './StreamEnd';
type ThreadChannel = StreamStart | StreamChunk | StreamEnd;
export default ThreadChannel;
