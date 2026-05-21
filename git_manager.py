"""
Git Manager - Git 操作封装（clone, pull, commit, push）
使用 GitPython 库，通过 SSH 认证
"""

import logging
import os
from pathlib import Path

import git

from config import GITHUB_REPO_URL

logger = logging.getLogger(__name__)


def ensure_repo_cloned(local_path: str) -> git.Repo:
    """
    确保仓库已克隆到本地。
    
    - 如果本地已有仓库：执行 git pull 拉取最新
    - 如果本地没有：执行 git clone
    
    Args:
        local_path: 本地仓库目录路径
        
    Returns:
        GitPython Repo 对象
        
    Raises:
        git.GitCommandError: Git 操作失败
    """
    repo_path = Path(local_path)
    
    if repo_path.exists() and (repo_path / ".git").exists():
        # 已有仓库，拉取最新
        repo = git.Repo(local_path)
        logger.info(f"Pulling latest from origin/main")
        try:
            repo.git.checkout("main")
            repo.remote("origin").pull("main")
            logger.info("Pull successful")
        except git.GitCommandError as e:
            logger.warning(f"Pull failed: {e}, continuing with local state")
        return repo
    
    # 需要克隆
    logger.info(f"Cloning {GITHUB_REPO_URL} -> {local_path}")
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        repo = git.Repo.clone_from(
            GITHUB_REPO_URL,
            local_path,
            branch="main",
        )
        logger.info("Clone successful")
        return repo
    except git.GitCommandError as e:
        logger.error(f"Clone failed: {e}")
        logger.error(
            "Make sure the GitHub repo exists: "
            f"https://github.com/{GITHUB_REPO_URL.split(':')[1].replace('.git', '')}"
        )
        raise


def commit_and_push_episode(
    repo: git.Repo,
    audio_path: str,
    rss_path: str,
    episode_date: str,
) -> None:
    """
    暂存音频、RSS和摘要存档，提交并推送到 main 分支。
    
    Args:
        repo: GitPython Repo 对象
        audio_path: 音频文件路径（相对于仓库根目录）
        rss_path: RSS 文件路径（相对于仓库根目录）
        episode_date: 日期标识 "YYYY-MM-DD"
        
    Raises:
        git.GitCommandError: Git 操作失败
    """
    repo_root = Path(repo.working_dir)
    
    # 转为相对路径
    audio_rel = os.path.relpath(audio_path, repo_root)
    rss_rel = os.path.relpath(rss_path, repo_root)
    
    # 摘要存档路径
    data_dir = repo_root / "data"
    summaries_file = data_dir / f"summaries_{episode_date}.json"
    archive_file = data_dir / "reports_archive" / f"reports_{episode_date}.json"
    
    files_to_add = [audio_rel, rss_rel]
    file_labels = [audio_rel, rss_rel]
    
    if summaries_file.exists():
        summaries_rel = os.path.relpath(str(summaries_file), repo_root)
        files_to_add.append(summaries_rel)
        file_labels.append(summaries_rel)
    
    if archive_file.exists():
        archive_rel = os.path.relpath(str(archive_file), repo_root)
        files_to_add.append(archive_rel)
        file_labels.append(archive_rel)
    
    logger.info(f"Staging: {', '.join(file_labels)}")
    
    # 暂存
    repo.index.add(files_to_add)
    
    # 提交
    commit_msg = f"Episode {episode_date} - 大宗商品研究周报"
    repo.index.commit(commit_msg)
    logger.info(f"Committed: {commit_msg}")
    
    # 推送
    logger.info("Pushing to origin/main...")
    repo.remote("origin").push("main")
    logger.info("Push successful")


def get_episode_count(repo: git.Repo) -> int:
    """
    从 audio/ 目录统计已有音频文件数量。
    
    Args:
        repo: GitPython Repo 对象
        
    Returns:
        音频文件数量
    """
    audio_dir = Path(repo.working_dir) / "audio"
    if not audio_dir.exists():
        return 0
    
    mp3_files = list(audio_dir.glob("ep_*.mp3"))
    return len(mp3_files)
