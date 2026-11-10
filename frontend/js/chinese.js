// OpenCC 字典随程序离线提供；只转换工作字幕，原 ASR / 剧本记录保持原样。
(() => {
  const convert = OpenCC.Converter({from:'t',to:'cn'});
  const cache = new Map();
  // 与 backend/core/srt_io.py 的导出规则一致：去掉标点，但保留数字里的 3.5 / 12:30、百分号和英文词内的连字符。
  function stripPunctuation(value) {
    const chars = [...String(value ?? '')];
    const out = [];
    for (let i = 0; i < chars.length; i++) {
      const char = chars[i];
      const previous = chars[i - 1] || '';
      const following = chars[i + 1] || '';
      const internal = ('.,:/'.includes(char) && /\d/.test(previous) && /\d/.test(following))
        || ("-'’".includes(char) && /[A-Za-z0-9]/.test(previous) && /[A-Za-z0-9]/.test(following));
      const unit = '%％‰'.includes(char) && /\d/.test(previous);
      if ((!/\p{P}/u.test(char) || internal || unit) && char!=='⋯') out.push(char);
    }
    return out.join('').replace(/[ \t]{2,}/g, ' ').trim();
  }
  // 与srt_io.normalize_subtitle_punctuation一致，数字/英文词内部的符号保持原样。
  function normalizePunctuation(value) {
    // 开启时保留参考本来的标点；不开启则走 stripPunctuation。
    return String(value ?? '').trim();
  }
  window.SubtitleChinese = {toSimplified(value) {
    const text=String(value ?? '');
    if(cache.has(text))return cache.get(text);
    const result=convert(text);
    if(cache.size>=4096)cache.clear();
    cache.set(text,result);return result;
  }, stripPunctuation, normalizePunctuation};
})();
