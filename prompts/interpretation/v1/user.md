$guidance

本次只需要写 **$segment_no 号段「$segment_title」** 的解读正文，内容性质：$segment_nature。

事实层 JSON（这是你能引用的全部事实；输入之外的一律不得出现）：

$facts_json

$correction
输出（JSON，键=$segment_no，只有这一个键）：

{"segments": {"$segment_no": "…"}}
