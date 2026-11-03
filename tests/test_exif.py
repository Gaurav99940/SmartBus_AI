import unittest
import io
from PIL import Image, ExifTags
from app.services.exif_service import _convert_to_degrees, compute_file_hash, extract_metadata

class TestExifMetadata(unittest.TestCase):
    def test_dms_conversion(self):
        # 37 degrees, 46 minutes, 29.64 seconds = 37.7749
        dms = (37, 46, 29.64)
        deg = _convert_to_degrees(dms)
        self.assertAlmostEqual(deg, 37.7749, places=4)

    def test_file_hash_computation(self):
        content1 = b"bus-passenger-test-image-content-1"
        content2 = b"bus-passenger-test-image-content-2"
        h1 = compute_file_hash(content1)
        h2 = compute_file_hash(content2)
        h1_repeat = compute_file_hash(content1)

        self.assertEqual(len(h1), 64) # SHA-256
        self.assertEqual(h1, h1_repeat)
        self.assertNotEqual(h1, h2)

    def test_extract_metadata_no_exif(self):
        # Create plain image without EXIF
        buf = io.BytesIO()
        img = Image.new("RGB", (50, 50), color=(100, 100, 100))
        img.save(buf, format="JPEG")
        buf.seek(0)

        meta = extract_metadata(buf)
        self.assertFalse(meta["has_exif"])
        self.assertIsNone(meta["latitude"])
        self.assertIsNone(meta["longitude"])
        self.assertGreater(len(meta["warnings"]), 0)

if __name__ == "__main__":
    unittest.main()
