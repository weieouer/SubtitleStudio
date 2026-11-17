# HDemucs 模型结构

`hdemucs.py` 来自 [TorchAudio v2.9.1](https://github.com/pytorch/audio/blob/v2.9.1/src/torchaudio/models/_hdemucs.py)，保留源文件的 MIT 许可证及版权声明。该结构只依赖 PyTorch；随源码固定，避免不同 TorchAudio 版本移除模型接口或音频 DLL 不匹配造成对齐辅助不可用。

模型权重由用户从辅模菜单按需下载，来源为 PyTorch 官方的 `hdemucs_high_trained.pt`，不放入 Git 仓库。该模型用于疑难句的人声辅助复查；原声对齐仍是默认流程。
