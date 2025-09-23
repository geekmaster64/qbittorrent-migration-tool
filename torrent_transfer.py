import qbittorrentapi
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def transfer_torrents_and_settings(source_host, source_user, source_pass, dest_host, dest_user, dest_pass):
    try:
        source_qbt = qbittorrentapi.Client(host=source_host, username=source_user, password=source_pass)
        source_qbt.auth_log_in()

        dest_qbt = qbittorrentapi.Client(host=dest_host, username=dest_user, password=dest_pass)
        dest_qbt.auth_log_in()

        # Fetch categories from source server
        source_categories = source_qbt.torrent_categories
        for cat_name, cat_data in source_categories.categories.items():
            if cat_name not in dest_qbt.torrent_categories.categories:
                try:
                    dest_qbt.torrent_categories.create_category(cat_name, save_path=cat_data.get('save_path', ''))
                    logger.info(f"Created category {cat_name} on destination server.")
                except Exception as e:
                    logger.error(f"Failed to create category {cat_name} on destination: {str(e)}")

        source_torrents = source_qbt.torrents_info()
        logger.info(f"Number of torrents to transfer: {len(source_torrents)}")

        for torrent in source_torrents:
            category = torrent.category
            logger.info(f"Processing torrent: {torrent.name}")
            
            # Assuming you can access the .torrent file from the source
            torrent_file_path = os.path.join(source_qbt.app_preferences()['save_path'], f"{torrent.hash}.torrent")
            try:
                with open(torrent_file_path, 'rb') as f:
                    torrent_data = f.read()
                new_torrent = dest_qbt.torrents_add(torrent_files=[torrent_data], category=category)
                logger.info(f"Added {torrent.name} to destination client with category {category}.")
            except FileNotFoundError:
                logger.error(f"Torrent file for {torrent.name} not found at {torrent_file_path}")
            except Exception as e:
                logger.error(f"Failed to add {torrent.name} to destination client: {str(e)}")

        logger.info("Torrent transfer process completed. Please check logs for any errors.")
        print("Torrent transfer process completed. Please check logs for any errors.")

    except Exception as e:
        logger.error(f"An error occurred during the transfer process: {str(e)}", exc_info=True)
        print(f"An error occurred during the transfer process: {str(e)}")

# Example usage
source_host = "ip-address:8080"
source_user = "username"
source_pass = "password"
dest_host = "ip-address:8080"
dest_user = "username"
dest_pass = "password"

transfer_torrents_and_settings(source_host, source_user, source_pass, dest_host, dest_user, dest_pass)